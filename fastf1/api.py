"""
:mod:`fastf1.api` - Api module
==============================

A collection of functions to interface with the F1 web api.
"""
import json
import base64
import zlib
from functools import reduce
import requests
import logging
import pandas as pd
import numpy as np


base_url = 'https://livetiming.formula1.com'

headers = {
  'Host': 'livetiming.formula1.com',
  'Connection': 'close',
  'Accept': '*/*',
  'User-Agent': 'Formula%201/715 CFNetwork/1120 Darwin/19.0.0',
  'Accept-Language': 'en-us',
  'Accept-Encoding': 'gzip, deflate',
  'X-Unity-Version': '2018.4.1f1'
}

pages = {
  'session_info': 'SessionInfo.json',  # more rnd
  'archive_status': 'ArchiveStatus.json',  # rnd=1880327548
  'heartbeat': 'Heartbeat.jsonStream',  # Probably time sinchronization?
  'audio_streams': 'AudioStreams.jsonStream',  # Link to audio commentary
  'driver_list': 'DriverList.jsonStream',  # Driver info and line story
  'extrapolated_clock': 'ExtrapolatedClock.jsonStream',  # Boolean
  'race_control_messages': 'RaceControlMessages.json',  #  Flags etc
  'session_status': 'SessionStatus.jsonStream',  # Start and finish times
  'team_radio': 'TeamRadio.jsonStream',  # Links to team radios
  'timing_app_data': 'TimingAppData.jsonStream',  # Tyres and laps (juicy)
  'timing_stats': 'TimingStats.jsonStream',  # 'Best times/speed' useless
  'track_status': 'TrackStatus.jsonStream',  # SC, VSC and Yellow
  'weather_data': 'WeatherData.jsonStream',  # Temp, wind and rain
  'position': 'Position.z.jsonStream',  # Coordinates, not GPS? (.z)
  'car_data': 'CarData.z.jsonStream',  # Telemetry channels (.z)
  'content_streams': 'ContentStreams.jsonStream',  # Lap by lap feeds
  'timing_data': 'TimingData.jsonStream',  # Gap to car ahead
  'lap_count': 'LapCount.jsonStream',  # Lap counter
  'championship_predicion': 'ChampionshipPrediction.jsonStream'  # Points
}
"""Known requests
"""


def make_path(wname, wdate, sname, sdate):
    """Create web path to append on livetiming.formula1.com for api
    requests.

    Args:
        wname: Weekend name (e.g. 'Italian Grand Prix')
        wdate: Weekend date (e.g. '2019-09-08')
        sname: Session name 'Qualifying' or 'Race'
        sdate: Session date (formatted as wdate)
    
    Returns:
        string path
    """
    smooth_operator = f'{wdate[:4]}/{wdate} {wname}/{sdate} {sname}/'
    return '/static/' + smooth_operator.replace(' ', '_')


def timing_data(path):
    """Timing data is a mixed stream of information of each driver.
    At a given time a packet of data may indicate position, lap time,
    speed trap, sector time and so on.

    While most of this data can be mapped lap by lap given a readable and
    usable data structure, other entries like position and time gaps are
    separated and mapped on finer timeseries.

    Args:
        path: url path (see :func:`make_path`)

    Returns:
        pandas.Dataframe for timing/lap data,
        pandas.Dataframe for position/time gaps
    """

    # possible optional sanity checks (TODO, maybe):
    #   - inlap has to be followed by outlap
    #   - pit stops may never be negative (missing outlap)
    #   - speed traps against telemetry (especially in Q FastLap - Slow Lap)

    response = fetch_page(path, 'timing_data')
    if response is None:
        raise SessionNotAvailableError("No data for this session! Are you sure this session wasn't cancelled?")

    # split up response per driver for easier iteration and processing later
    resp_per_driver = dict()
    for entry in response:
        if 'Lines' not in entry[1]:
            continue
        for drv in entry[1]['Lines']:
            if drv not in resp_per_driver.keys():
                resp_per_driver[drv] = [(entry[0], entry[1]['Lines'][drv])]
            else:
                resp_per_driver[drv].append((entry[0], entry[1]['Lines'][drv]))

    # define all empty columns
    empty_laps = {'Time': pd.NaT, 'Driver': str(), 'LapTime': pd.NaT, 'NumberOfLaps': np.NaN,
                  'NumberOfPitStops': np.NaN, 'PitOutTime': pd.NaT, 'PitInTime': pd.NaT,
                  'Sector1Time': pd.NaT, 'Sector2Time': pd.NaT, 'Sector3Time': pd.NaT,
                  'Sector1SessionTime': pd.NaT, 'Sector2SessionTime': pd.NaT, 'Sector3SessionTime': pd.NaT,
                  'SpeedI1': np.NaN, 'SpeedI2': np.NaN, 'SpeedFL': np.NaN, 'SpeedST': np.NaN}

    empty_stream = {'Time': pd.NaT, 'Driver': str(), 'Position': np.NaN,
                    'GapToLeader': np.NaN, 'IntervalToPositionAhead': np.NaN}

    # create empty data dicts and populate them with data from all drivers after that
    laps_data = {key: list() for key, val in empty_laps.items()}
    stream_data = {key: list() for key, val in empty_stream.items()}

    for drv in resp_per_driver.keys():
        drv_laps_data = _laps_data_driver(resp_per_driver[drv], empty_laps, drv)
        drv_stream_data = _stream_data_driver(resp_per_driver[drv], empty_stream, drv)

        for key in empty_laps.keys():
            laps_data[key].extend(drv_laps_data[key])

        for key in empty_stream.keys():
            stream_data[key].extend(drv_stream_data[key])

    laps_data = pd.DataFrame(laps_data)
    stream_data = pd.DataFrame(stream_data)

    if ((laps_data.to_numpy() == '') | (pd.isna(laps_data.to_numpy()))).all():  # if all values of the frame are nan/...
        raise SessionNotAvailableError("No data for this session! Are you sure this session wasn't cancelled?")

    return laps_data, stream_data


def _laps_data_driver(driver_raw, empty_vals, drv):
    """
    Data is on a per-lap basis.

    Boolean flag 'PitOut' is not evaluated. Meaning is unknown and flag is only sometimes present when a car leaves
    the pits.

    Params:
        driver_raw (list): raw api response for this driver only [(Timestamp, data), (...), ...]
        empty_vals (dict): dictionary of column names and empty column values
        drv (str): driver identifier

    Returns:
         dictionary of laps data for this driver
    """

    # do a quick first pass over the data to find out when laps start and end
    # this is needed so we can work with a more efficient "look ahead" on the main pass
    # example: we can have 'PitOut' 0.01s before a new lap starts, but 'PitOut' belongs to the new lap, not the old one

    lapcnt = 0  # we're keeping two separate lap counts because sometimes the api has a non existent lap too much...
    api_lapcnt = 0  # ...at the beginning; we can correct that though;
    # api_lapcnt does not count backwards even if the source data does
    in_past = False  # flag for when the data went back in time
    out_of_pit = False  # flag set to true when driver drives out FOR THE FIRST TIME; stays true from then on

    # entries are prefilled with empty values and only overwritten if they exist in the response line
    drv_data = {key: [val, ] for key, val in empty_vals.items()}

    for time, resp in driver_raw:
        data_in_lap = False
        # the first three ifs are just edge case handling for the rare sessions were the data goes back in time
        if in_past and 'NumberOfLaps' in resp and resp['NumberOfLaps'] == api_lapcnt:
            in_past = False  # we're back in the present

        if 'NumberOfLaps' in resp and resp['NumberOfLaps'] < api_lapcnt:
            logging.warning(f"The api attempted to rewrite history for driver {drv}. This was ignored! The data may not"
                            f" be entirely correct. (near lap {lapcnt})")
            in_past = True
            continue

        if in_past:  # still in the past, just continue and ignore everything
            continue

        if ('InPit' in resp) and (resp['InPit'] is False):
            out_of_pit = True  # drove out of the pits for the first time

        # new lap; create next row
        if 'NumberOfLaps' in resp and resp['NumberOfLaps'] > api_lapcnt:
            api_lapcnt += 1
            # make sure the car actually drove out of the pits already; it can't be a new lap if it didn't
            if out_of_pit:
                drv_data['Time'][lapcnt] = _to_timedelta(time)
                lapcnt += 1
                # append a new empty row; last row may not be populated (depending on session) and may be removed later
                for key, val in empty_vals.items():
                    drv_data[key].append(val)

    # now, do the main pass where all the other data is actually filled in
    # same counters and flags as before, reset them
    lapcnt = 0  # we're keeping two separate lap counts because sometimes the api has a non existent lap too much...
    api_lapcnt = 0  # ...at the beginning; we can correct that though;
    # api_lapcnt does not count backwards even if the source data does
    in_past = False  # flag for when the data went back in time

    pitstops = -1  # start with -1 because first is out lap, needs to be zero after that

    # iterate through the data; new lap triggers next row in data
    for time, resp in driver_raw:
        # the first three ifs are just edge case handling for the rare sessions were the data goes back in time
        if in_past and 'NumberOfLaps' in resp and resp['NumberOfLaps'] == api_lapcnt:
            in_past = False  # we're back in the present
        if in_past or ('NumberOfLaps' in resp and resp['NumberOfLaps'] < api_lapcnt):
            in_past = True
            continue

        if (lapcnt == 0) and ((drv_data['Time'][lapcnt] - _to_timedelta(time)) > pd.Timedelta(5, 'min')):
            # ignore any data which arrives more than 5 minutes before the end of the first lap, except 'PitOut'
            if ('InPit' in resp) and (resp['InPit'] is False):
                drv_data['PitOutTime'][lapcnt] = _to_timedelta(time)
                pitstops = 0  # special here, can be multiple times for no reason therefore set zero instead of +=1
            continue

        # values which are up to five seconds late are still counted towards the previous lap
        # (sector times, speed traps and lap times)
        lap_offset = 0
        if (lapcnt > 0) and (_to_timedelta(time) - drv_data['Time'][lapcnt - 1] < pd.Timedelta(5, 's')):
            lap_offset = 1

        if 'Sectors' in resp and isinstance(resp['Sectors'], dict):
            # sometimes it's a list but then it never contains values...
            for sn, sector, sesst in (('0', 'Sector1Time', 'Sector1SessionTime'),
                                      ('1', 'Sector2Time', 'Sector2SessionTime'),
                                      ('2', 'Sector3Time', 'Sector3SessionTime')):
                if val := _dict_get(resp, 'Sectors', sn, 'Value'):
                    drv_data[sector][lapcnt - lap_offset] = _to_timedelta(val)
                    drv_data[sesst][lapcnt - lap_offset] = _to_timedelta(time)

        if val := _dict_get(resp, 'LastLapTime', 'Value'):
            # if 'LastLapTime' is received less than five seconds after the start of a new lap, it is still added
            # to the last lap
            drv_data['LapTime'][lapcnt - lap_offset] = _to_timedelta(val)

        if 'Speeds' in resp:
            for trapkey, trapname in (('I1', 'SpeedI1'), ('I2', 'SpeedI2'), ('FL', 'SpeedFL'), ('ST', 'SpeedST')):
                if val := _dict_get(resp, 'Speeds', trapkey, 'Value'):
                    # speed has to be float because int does not support NaN
                    drv_data[trapname][lapcnt - lap_offset] = float(val)

        if 'InPit' in resp:
            # 'InPit': True is received once when entering pits, False is received once when leaving
            if resp['InPit'] is True:
                if pitstops >= 0:
                    drv_data['PitInTime'][lapcnt] = _to_timedelta(time)
            elif ('NumberOfLaps' in resp) or (drv_data['Time'][lapcnt] - _to_timedelta(time)) < pd.Timedelta(5, 's'):
                # same response line as beginning of next lap or beginning of next lap less than 5 seconds away
                drv_data['PitOutTime'][lapcnt+1] = _to_timedelta(time)  # add to next lap
                pitstops += 1
            else:
                drv_data['PitOutTime'][lapcnt] = _to_timedelta(time)  # add to current lap
                pitstops += 1

        # new lap; create next row
        if 'NumberOfLaps' in resp and resp['NumberOfLaps'] > api_lapcnt:
            api_lapcnt += 1
            # make sure the car actually drove out of the pits already; it can't be a new lap if it didn't
            if pitstops >= 0:
                drv_data['Time'][lapcnt] = _to_timedelta(time)
                drv_data['NumberOfLaps'][lapcnt] = lapcnt + 1  # don't use F1's lap count; ours is better
                drv_data['NumberOfPitStops'][lapcnt] = pitstops
                drv_data['Driver'][lapcnt] = drv
                lapcnt += 1

    if lapcnt == 0:  # no data at all for this driver
        return drv_data

    # done reading the data, do postprocessing

    def data_in_lap(lap_n):
        relevant = ('Sector1Time', 'Sector2Time', 'Sector3Time', 'SpeedI1', 'SpeedI2',
                    'SpeedFL', 'SpeedST', 'LapTime')
        for col in relevant:
            if not pd.isnull(drv_data[col][lap_n]):
                return True
        return False

    # 'NumberOfLaps' always introduces a new lap (can be a previous one) but sometimes there is one more lap at the end
    # in this case the data will be added as usual above, lap count and pit stops are added here and the 'Time' is
    # calculated below from sector times
    if data_in_lap(lapcnt):
        drv_data['NumberOfLaps'][lapcnt] = lapcnt + 1
        drv_data['NumberOfPitStops'][lapcnt] = pitstops
        drv_data['Driver'][lapcnt] = drv
    else:  # if there was no more data after the last lap count increase, delete the last empty record
        for key in drv_data.keys():
            drv_data[key] = drv_data[key][:-1]
    if not data_in_lap(0):  # remove first lap if there's no data; "pseudo outlap" that didn't exist
        for key in drv_data.keys():
            drv_data[key] = drv_data[key][1:]
        drv_data['NumberOfLaps'] = list(map(lambda n: n-1, drv_data['NumberOfLaps']))  # reduce each lap count by one

    # lap time sync; check which sector time was triggered with the lowest latency
    # Sector3SessionTime == end of lap
    # Sector2SessionTime + Sector3Time == end of lap
    # Sector1SessionTime + Sector2Time + Sector3Time == end of lap
    # all of these three have slightly different times; take earliest one -> most exact because can't trigger too early
    for i in range(len(drv_data['Time'])):
        sector_sum = pd.Timedelta(0)
        min_time = drv_data['Time'][i]
        for sector_time, session_time in ((pd.Timedelta(0), drv_data['Sector3SessionTime'][i]),
                                          (drv_data['Sector3Time'][i], drv_data['Sector2SessionTime'][i]),
                                          (drv_data['Sector2Time'][i], drv_data['Sector1SessionTime'][i])):
            if pd.isnull(session_time):
                continue
            if pd.isnull(sector_time):
                break  # need to stop here because else the sector sum will be incorrect

            sector_sum += sector_time
            new_time = session_time + sector_sum
            if not pd.isnull(new_time) and (new_time < min_time or pd.isnull(min_time)):
                min_time = new_time
        drv_data['Time'][i] = min_time

    # one last check
    # last lap needs to be removed if it does not have a 'Time' and it could not be calculated (likely an inlap)
    if pd.isnull(drv_data['Time'][-1]):
        for key in drv_data.keys():
            drv_data[key] = drv_data[key][:-1]

    return drv_data


def _stream_data_driver(driver_raw, empty_vals, drv):
    """
    Data is on a timestamp basis.

    Params:
        driver_raw (list): raw api response for this driver only [(Timestamp, data), (...), ...]
        empty_vals (dict): dictionary of column names and empty column values
        drv (str): driver identifier

    Returns:
         dictionary of timing stream data for this driver
    """
    # entries are prefilled with empty or previous values and only overwritten if they exist in the response line
    # basically interpolation by filling up with last known value because not every value is in every response
    drv_data = {key: [val, ] for key, val in empty_vals.items()}
    i = 0

    # iterate through the data; timestamp + any of the values triggers new row in data
    for time, resp in driver_raw:
        new_entry = False
        if val := _dict_get(resp, 'Position'):
            drv_data['Position'][i] = val
            new_entry = True
        if val := _dict_get(resp, 'GapToLeader'):
            drv_data['GapToLeader'][i] = val
            new_entry = True
        if val := _dict_get(resp, 'IntervalToPositionAhead', 'Value'):
            drv_data['IntervalToPositionAhead'][i] = val
            new_entry = True

        # at least one value was present, create next row
        if new_entry:
            drv_data['Time'][i] = time
            drv_data['Driver'][i] = drv
            i += 1

            # create next row of data from the last values; there will always be one row too much at the end which is
            # removed again
            for key, val in empty_vals.items():
                drv_data[key].append(drv_data[key][-1])

    for key in drv_data.keys():
        drv_data[key] = drv_data[key][:-1]  # remove very last row again

    return drv_data


def timing_app_data(path, response=None):
    """Full parse of timing app data. This parsing is quite ignorant,
    with  minimum logic just to fix data structure inconsistencies. Tyre
    information is passed to the summary table.

    Args:
        path (str): web path for base_url, see :func:`make_path`
        response (optional): api response can be passed if data was already downloaded

    Returns:
        pandas.Dataframe
    """
    if response is None:
        response = fetch_page(path, 'timing_app_data')

    data = {'LapNumber': [], 'Driver': [], 'LapTime': [], 'Stint': [], 'TotalLaps': [], 'Compound': [], 'New': [],
            'TyresNotChanged': [], 'Time': [], 'LapFlags': [], 'LapCountTime': [], 'StartLaps': [], 'Outlap': []}

    for entry in response:
        time = _to_timedelta(entry[0])

        row = entry[1]
        for driver_number in row['Lines']:
            if update := _dict_get(row, 'Lines', driver_number, 'Stints'):
                for stint_number, stint in enumerate(update):
                    if isinstance(update, dict):
                        stint_number = int(stint)
                        stint = update[stint]
                    for key in data:
                        if key in stint:
                            data[key].append(stint[key])
                        else:
                            data[key].append(None)
                    for key in stint:
                        if key not in data:
                            logging.debug(f"Found unknown key in timing app data: {key}")

                    data['Time'][-1] = time
                    data['Driver'][-1] = driver_number
                    data['Stint'][-1] = stint_number

    return pd.DataFrame(data)


def car_data(path):
    """Fetch and create pandas dataframe for each driver containing
    Telemetry data.

    Samples are not synchronised with the other dataframes and sampling
    time is not constant, usually 240ms but sometimes can be ~270ms.
    Keep absolute reference.

    Dataframe columns:
        - Date (pandas.Timestamp): timestamp for this sample as Date + Time; more or less exact
        - Time (pandas.Timedelta): session timestamp; inaccurate, has duplicate values; use Date instead
        - Speed (int): Km/h
        - RPM (int)
        - Gear (int)
        - Throttle (int): 0-100%
        - Brake (int): 0-100% (don't trust brake too much)
        - DRS (int): 0=Off, 8=Active; sometimes other values --> to be researched still

    Args:
        path: url path (see :func:`make_path`)

    Returns:
        dictionary containing one pandas dataframe for each driver; dictionary keys are driver numbers as string
    """
    logging.info("Fetching car data")
    raw = fetch_page(path, 'car_data')
    logging.info("Parsing car data")

    channels = {'0': 'RPM', '2': 'Speed', '3': 'nGear', '4': 'Throttle', '5': 'Brake', '45': 'DRS'}
    columns = {'Time', 'Date', 'RPM', 'Speed', 'nGear', 'Throttle', 'Brake', 'DRS'}
    date_format = "%Y-%m-%dT%H:%M:%S.%f%z"

    data = dict()

    for line in raw:
        time = _to_timedelta(line[0])
        for entry in line[1]['Entries']:
            date = pd.to_datetime(entry['Utc'], format=date_format)

            for driver in entry['Cars']:
                if driver not in data:
                    data[driver] = {col: list() for col in columns}

                data[driver]['Time'].append(time)
                data[driver]['Date'].append(date)

                for n in channels:
                    val = _dict_get(entry, 'Cars', driver, 'Channels', n)
                    if not val:
                        val = 0
                    data[driver][channels[n]].append(val)

    # create one dataframe per driver and check for the longest dataframe
    most_complete_ref = None
    for driver in data:
        data[driver] = pd.DataFrame(data[driver])  # convert dict to dataframe
        # check length of dataframe; sometimes there can be missing data
        if most_complete_ref is None or len(data[driver]['Date']) > len(most_complete_ref):
            most_complete_ref = data[driver]['Date']

    # if everything is well, all dataframes should have the same length and no postprocessing is necessary
    for driver in data:
        if len(data[driver]['Date']) < len(most_complete_ref):
            # there is missing data for this driver
            # extend the Date column and fill up missing telemetry values with zero,
            # except Time which is left as NaT and will be calculated correctly during resampling
            index_df = pd.DataFrame(data={'Date': most_complete_ref})
            data[driver] = data[driver].merge(index_df, how='outer').sort_values(by='Date').reset_index(drop=True)
            data[driver].loc[:, channels.values()] = data[driver].loc[:, channels.values()].fillna(value=0, inplace=False)

            logging.warning(f"Car data for driver {driver} is incomplete!")

    return data


def position(path):
    """Fetch and create pandas dataframe for Position.

    Samples are not synchronised with the other dataframes and sampling
    time is not constant, usually 300ms but sometimes can be ~200ms.
    Keep absolute reference.

    Dataframe columns:
        - Date (pandas.Timestamp): timestamp for this sample as Date + Time; more or less exact
        - Time (pandas.Timedelta): session timestamp; inaccurate, has duplicate values; use Date instead
        - X, Y, Z (int): Position coordinates
        - Status (str): 'OnTrack' or 'OffTrack'

    Args:
        path: web path for base_url, see :func:`make_path`

    Returns:
        dictionary containing one pandas dataframe for each driver, dictionary keys are driver numbers as string
    """
    logging.info("Fetching position")
    raw = fetch_page(path, 'position')
    logging.info("Parsing position") 

    if not raw:
        return {}

    ts_length = 12  # length of timestamp: len('00:00:00:000')
    date_format = "%Y-%m-%dT%H:%M:%S.%f"
    columns = ['Time', 'Date', 'Status', 'X', 'Y', 'Z']

    data = dict()

    for record in raw:
        time = _to_timedelta(record[:ts_length])
        jrecord = parse(record[ts_length:], zipped=True)

        for sample in jrecord['Position']:
            date = pd.to_datetime(sample['Timestamp'], format=date_format)

            for driver in sample['Entries']:
                if driver not in data:
                    data[driver] = {col: list() for col in columns}

                data[driver]['Time'].append(time)
                data[driver]['Date'].append(date)

                for coord in ['X', 'Y', 'Z']:
                    data[driver][coord].append(_dict_get(sample, 'Entries', driver, coord))

                status = _dict_get(sample, 'Entries', driver, 'Status')
                if str(status).isdigit():
                    # Fallback on older api status mapping and convert
                    status = 'OffTrack' if int(status) else 'OnTrack'
                data[driver]['Status'].append(status)

    # create one dataframe per driver and check for the longest dataframe
    most_complete_ref = None
    for driver in data:
        data[driver] = pd.DataFrame(data[driver])  # convert dict to dataframe
        # check length of dataframe; sometimes there can be missing data
        if most_complete_ref is None or len(data[driver]['Date']) > len(most_complete_ref):
            most_complete_ref = data[driver]['Date']

    # if everything is well, all dataframes should have the same length and no postprocessing is necessary
    for driver in data:
        if len(data[driver]['Date']) < len(most_complete_ref):
            # there is missing data for this driver
            # extend the Date column and fill up missing telemetry values with zero,
            # except Time which is left as NaT and will be calculated correctly during resampling
            # and except Status which should be 'OffTrack' for missing data
            index_df = pd.DataFrame(data={'Date': most_complete_ref})
            data[driver] = data[driver].merge(index_df, how='outer').sort_values(by='Date').reset_index()
            data[driver]['Status'].fillna(value='OffTrack', inplace=True)
            data[driver].loc[:, ['X', 'Y', 'Z']] = data[driver].loc[:, ['X', 'Y', 'Z']].fillna(value=0, inplace=False)

            logging.warning(f"Position data for driver {driver} is incomplete!")

    return data


def fetch_page(path, name):
    """Fetch formula1 web api, given url path and page name. An attempt
    to parse json or decode known messages is made.

    Args:
        path: url path (see :func:`make_path`)
        name: page name (see :attr:`pages`)

    Returns:
        dictionary if content was json, list of entries if jsonStream,
        where each element is len 2: [clock, content]. Content is
        parsed with :func:`parse`. None if request failed.

    """
    page = pages[name]
    is_stream = 'jsonStream' in page
    is_z = '.z.' in page
    r = requests.get(base_url + path + pages[name], headers=headers)
    if r.status_code == 200:
        raw = r.content.decode('utf-8-sig')
        if is_stream:
            records = raw.split('\r\n')[:-1]  # last split is empty
            if name == 'position':
                # Special case to improve memory efficency
                return records
            else:
                tl = 12  # length of timestamp: len('00:00:00:000')
                return [[e[:tl], parse(e[tl:], zipped=is_z)] for e in records]
        else:
            return parse(raw, is_z)
    else:
        return None


def parse(text, zipped=False):
    """Parse json and jsonStream as known from livetiming.formula1.com
    """
    if text[0] == '{':
        return json.loads(text)
    if text[0] == '"':
        text = text.strip('"')
    if zipped:
        text = zlib.decompress(base64.b64decode(text), -zlib.MAX_WBITS)
        return parse(text.decode('utf-8-sig'))
    logging.warning("Couldn't parse text")
    return text


def _to_timedelta(x):
    if len(x) and isinstance(x, str):
        return pd.to_timedelta('00:00:00.000'[:-len(x)] + x)
    return pd.to_timedelta(x)


def _dict_get(d, *keys):
    """Recursive dict get. Can take an arbitrary number of keys and returns an empty
    dict if any key does not exist.
    https://stackoverflow.com/a/28225747"""
    return reduce(lambda c, k: c.get(k, {}), keys, d)


class SessionNotAvailableError(BaseException):
    """Raised if an api request returned no data for the requested session.
    A likely cause is that the session does not exist because it was cancelled."""
    def __init__(self, *args):
        super().__init__(*args)
