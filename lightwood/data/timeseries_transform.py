import copy
import datetime
import dateutil
import numpy as np
import pandas as pd
import multiprocessing as mp
from lightwood.helpers.parallelism import get_nr_procs
from functools import partial
from typing import Dict
from lightwood.api.types import TimeseriesSettings
from lightwood.helpers.log import log
from lightwood.api import dtype


def transform_timeseries(
        data: pd.DataFrame, dtype_dict: Dict[str, str],
        timeseries_settings: TimeseriesSettings, target: str, mode: str) -> pd.DataFrame:
    tss = timeseries_settings
    original_df = copy.deepcopy(data)
    gb_arr = tss.group_by if tss.group_by is not None else []
    ob_arr = tss.order_by
    window = tss.window

    if '__mdb_make_predictions' in original_df.columns:
        index = original_df[original_df['__mdb_make_predictions'].map(
            {'True': True, 'False': False, True: True, False: False}).isin([True])]
        infer_mode = index.shape[0] == 0  # condition to trigger: __mdb_make_predictions is set to False everywhere
        # @TODO: dont drop and use instead of original_index?
        original_df = original_df.reset_index(drop=True) if infer_mode else original_df
    else:
        infer_mode = False

    original_index_list = []
    idx = 0
    for row in original_df.itertuples():
        if _make_pred(row) or infer_mode:
            original_index_list.append(idx)
            idx += 1
        else:
            original_index_list.append(None)

    original_df['original_index'] = original_index_list

    secondary_type_dict = {}
    for col in ob_arr:
        if dtype_dict[col] in (dtype.date, dtype.integer, dtype.float):
            secondary_type_dict[col] = dtype_dict[col]

    # Convert order_by columns to numbers (note, rows are references to mutable rows in `original_df`)
    for _, row in original_df.iterrows():
        for col in ob_arr:
            # @TODO: Remove if the TS encoder can handle `None`
            if row[col] is None or pd.isna(row[col]):
                row[col] = 0.0
            else:
                if dtype_dict[col] == dtype.date:
                    try:
                        row[col] = dateutil.parser.parse(
                            row[col],
                            # transaction.lmd.get('dateutil_parser_kwargs_per_column', {}).get(col, {}) # @TODO
                            **{}
                        )
                    except (TypeError, ValueError):
                        pass

                if isinstance(row[col], datetime.datetime):
                    row[col] = row[col].timestamp()

                try:
                    row[col] = float(row[col])
                except ValueError:
                    raise ValueError(f'Failed to order based on column: "{col}" due to faulty value: {row[col]}')

    for oby in tss.order_by:
        original_df[f'__mdb_original_{oby}'] = original_df[oby]

    group_lengths = []
    if len(gb_arr) > 0:
        df_arr = []
        for _, df in original_df.groupby(gb_arr):
            df_arr.append(df.sort_values(by=ob_arr))
            group_lengths.append(len(df))
    else:
        df_arr = [original_df]
        group_lengths.append(len(original_df))

    n_groups = len(df_arr)
    last_index = original_df['original_index'].max()
    for i, subdf in enumerate(df_arr):
        if '__mdb_make_predictions' in subdf.columns and mode == 'predict':
            if infer_mode:
                df_arr[i] = _ts_infer_next_row(subdf, ob_arr, last_index)
                last_index += 1

    if len(original_df) > 500:
        # @TODO: restore possibility to override this with args
        nr_procs = get_nr_procs(original_df)
        log.info(f'Using {nr_procs} processes to reshape.')
        pool = mp.Pool(processes=nr_procs)
        # Make type `object` so that dataframe cells can be python lists
        df_arr = pool.map(partial(_ts_to_obj, historical_columns=ob_arr + tss.historical_columns), df_arr)
        df_arr = pool.map(partial(_ts_order_col_to_cell_lists,
                          historical_columns=ob_arr + tss.historical_columns), df_arr)
        df_arr = pool.map(
            partial(
                _ts_add_previous_rows, historical_columns=ob_arr + tss.historical_columns, window=window),
            df_arr)

        df_arr = pool.map(partial(_ts_add_future_target, target=target, nr_predictions=tss.nr_predictions,
                                  data_dtype=tss.target_type, mode=mode),
                          df_arr)

        if tss.use_previous_target:
            df_arr = pool.map(
                partial(
                    _ts_add_previous_target, target=target,
                    window=tss.window, data_dtype=tss.target_type),
                df_arr)
        pool.close()
        pool.join()
    else:
        for i in range(n_groups):
            df_arr[i] = _ts_to_obj(df_arr[i], historical_columns=ob_arr + tss.historical_columns)
            df_arr[i] = _ts_order_col_to_cell_lists(df_arr[i], historical_columns=ob_arr + tss.historical_columns)
            df_arr[i] = _ts_add_previous_rows(df_arr[i],
                                              historical_columns=ob_arr + tss.historical_columns, window=window)
            df_arr[i] = _ts_add_future_target(df_arr[i], target=target, nr_predictions=tss.nr_predictions,
                                              data_dtype=tss.target_type, mode=mode)
            if tss.use_previous_target:
                df_arr[i] = _ts_add_previous_target(df_arr[i], target=target, window=tss.window,
                                                    data_dtype=tss.target_type)

    combined_df = pd.concat(df_arr)

    if '__mdb_make_predictions' in combined_df.columns:
        combined_df = pd.DataFrame(combined_df[combined_df['__mdb_make_predictions'].astype(bool).isin([True])])
        del combined_df['__mdb_make_predictions']

    if not infer_mode and any([i < tss.window for i in group_lengths]):
        if tss.allow_incomplete_history:
            log.warning("Forecasting with incomplete historical context, predictions might be subpar")
        else:
            raise Exception(f'Not enough historical context to make a timeseries prediction. Please provide a number of rows greater or equal to the window size. If you can\'t get enough rows, consider lowering your window size. If you want to force timeseries predictions lacking historical context please set the `allow_incomplete_history` timeseries setting to `True`, but this might lead to subpar predictions.') # noqa

    df_gb_map = None
    if n_groups > 1:
        df_gb_list = list(combined_df.groupby(tss.group_by))
        df_gb_map = {}
        for gb, df in df_gb_list:
            df_gb_map['_' + '_'.join(gb)] = df

    timeseries_row_mapping = {}
    idx = 0

    if df_gb_map is None:
        for _, row in combined_df.iterrows():
            if not infer_mode:
                timeseries_row_mapping[idx] = int(
                    row['original_index']) if row['original_index'] is not None and not np.isnan(
                    row['original_index']) else None
            else:
                timeseries_row_mapping[idx] = idx
            idx += 1
    else:
        for gb in df_gb_map:
            for _, row in df_gb_map[gb].iterrows():
                if not infer_mode:
                    timeseries_row_mapping[idx] = int(
                        row['original_index']) if row['original_index'] is not None and not np.isnan(
                        row['original_index']) else None
                else:
                    timeseries_row_mapping[idx] = idx

                idx += 1

    del combined_df['original_index']

    # return combined_df, secondary_type_dict, timeseries_row_mapping, df_gb_map
    return combined_df


def _ts_infer_next_row(df, ob, last_index):
    last_row = df.iloc[[-1]].copy()
    if df.shape[0] > 1:
        butlast_row = df.iloc[[-2]]
        delta = (last_row[ob].values - butlast_row[ob].values).flatten()[0]
    else:
        delta = 1
    last_row.original_index = None
    last_row.index = [last_index + 1]
    last_row['__mdb_make_predictions'] = True
    last_row['__mdb_ts_inferred'] = True
    last_row[ob] += delta
    return df.append(last_row)


def _make_pred(row):
    return not hasattr(row, '__mdb_make_predictions') or row.make_predictions


def _ts_to_obj(df, historical_columns):
    for hist_col in historical_columns:
        df.loc[:, hist_col] = df[hist_col].astype(object)
    return df


def _ts_order_col_to_cell_lists(df, historical_columns):
    for order_col in historical_columns:
        for ii in range(len(df)):
            label = df.index.values[ii]
            df.at[label, order_col] = [df.at[label, order_col]]
    return df


def _ts_add_previous_rows(df, historical_columns, window):
    for order_col in historical_columns:
        for i in range(len(df)):
            previous_indexes = [*range(max(0, i - window), i)]

            for prev_i in reversed(previous_indexes):
                df.iloc[i][order_col].append(
                    df.iloc[prev_i][order_col][-1]
                )

            # Zero pad
            # @TODO: Remove since RNN encoder can do without (???)
            df.iloc[i][order_col].extend(
                [0] * (1 + window - len(df.iloc[i][order_col]))
            )
            df.iloc[i][order_col].reverse()
    return df


def _ts_add_previous_target(df, target, window, data_dtype):
    if target not in df:
        return df
    previous_target_values = list(df[target])
    del previous_target_values[-1]
    previous_target_values = [None] + previous_target_values

    previous_target_values_arr = []
    for i in range(len(previous_target_values)):
        prev_vals = previous_target_values[max(i - window, 0):i + 1]
        arr = [None] * (window - len(prev_vals) + 1)
        arr.extend(prev_vals)
        previous_target_values_arr.append(arr)

    df[f'__mdb_ts_previous_{target}'] = previous_target_values_arr
    return df


def _ts_add_future_target(df, target, nr_predictions, data_dtype, mode):
    if target not in df:
        return df
    if data_dtype in (dtype.integer, dtype.float, dtype.array, dtype.tsarray):
        df[target] = df[target].astype(float)

    for timestep_index in range(1, nr_predictions):
        next_target_value_arr = list(df[target])
        for del_index in range(0, min(timestep_index, len(next_target_value_arr))):
            del next_target_value_arr[0]
            next_target_value_arr.append(None)
        col_name = f'{target}_timestep_{timestep_index}'
        df[col_name] = next_target_value_arr
        df[col_name] = df[col_name].fillna(value=np.nan)

    # drop rows with incomplete target info.
    if mode == 'train':
        for col in [f'{target}_timestep_{i}' for i in range(1, nr_predictions)]:
            if '__mdb_make_predictions' not in df.columns:
                df['__mdb_make_predictions'] = True
            df.loc[df[col].isna(), ['__mdb_make_predictions']] = False

    return df
