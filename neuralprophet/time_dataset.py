import logging
from collections import OrderedDict, defaultdict
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
import torch
from torch.utils.data.dataset import Dataset

from neuralprophet import configure, utils
from neuralprophet.df_utils import get_max_num_lags
from neuralprophet.hdays_utils import get_country_holidays

log = logging.getLogger("NP.time_dataset")


class GlobalTimeDataset(Dataset):
    def __init__(self, df, **kwargs):
        """Initialize Timedataset from time-series df.
        Parameters
        ----------
            df : pd.DataFrame
                dataframe containing column ``ds``, ``y``, and optionally``ID`` and
                normalized columns normalized columns ``ds``, ``y``, ``t``, ``y_scaled``
            **kwargs : dict
                Identical to :meth:`tabularize_univariate_datetime`
        """
        # # TODO (future): vectorize
        timedatasets = [TimeDataset(df_i, df_name, **kwargs) for df_name, df_i in df.groupby("ID")]
        self.combined_timedataset = [item for timedataset in timedatasets for item in timedataset]
        self.length = sum(timedataset.length for timedataset in timedatasets)

    def __len__(self):
        return self.length

    def __getitem__(self, idx):
        return self.combined_timedataset[idx]


class TimeDataset(Dataset):
    """Create a PyTorch dataset of a tabularized time-series"""

    def __init__(self, df, name, **kwargs):
        """Initialize Timedataset from time-series df.
        Parameters
        ----------
            df : pd.DataFrame
                Time series data
            name : str
                Name of time-series
            **kwargs : dict
                Identical to :meth:`tabularize_univariate_datetime`
        """
        ## Outcome after a call to init (summary):
        # - add events and holidays columns to df
        # - calculated the number of usable samples (accounting for nan and filters)
        # - creates mapping of sample index to df index

        ## Context Notes
        # Currently done to df before it arrives here:
        # -> fit calls prep_or_copy_df, _check_dataframe, and _handle_missing_data, passes to _train
        # -> _train calls prep_or_copy_df, then passes to init_train_loader, which returns the train_loader
        # -> init_train_loader calls prep_or_copy_df, _normalize, _create_dataset (returns TimeDataset), returns dataset wrapped in DataLoader
        # ->_create_dataset calls prep_or_copy_df, then returns GlobalTimeDataset
        # Future TODO: integrate these preprocessing steps happening outside?

        self.df = df
        self.name = name
        self.meta = OrderedDict({})
        self.meta["df_name"] = self.name
        self.config_args = kwargs

        # TODO: Preprocessing of features (added to self.df)
        # - events and holidays: convert date-time occurence dictionary to a column of values in the self.df
        # - These will then be later tabularized in __get_item___

        ## TODO Create index mapping of sample index to df index
        # - Filter missing samples and prediction frequency (does not actually drop, but creates indexmapping)
        # -- filter samples
        # analogous to `self.filter_samples_after_init(self.kwargs["prediction_frequency"])`
        # -- drop nan
        # analogous to `self.drop_nan_after_init(self.df, self.kwargs["predict_steps"], self.kwargs["config_missing"].drop_missing)
        # save the created mapping to self.sample2index_map (used by self.sample2index_map)
        self.sample2index_map, self.length = self.create_sample2index_map(df)

    def __getitem__(self, index):
        """Overrides parent class method to get an item at index.
        Parameters
        ----------
            index : int
                Sample location in dataset
        Returns
        -------
        OrderedDict
            Model inputs, each of len(df) but with varying dimensions
            Note
            ----
            Contains the following data:
            Model Inputs
                * ``time`` (np.array, float), dims: (num_samples, 1)
                * ``seasonalities`` (OrderedDict), named seasonalities
                each with features (np.array, float) - dims: (num_samples, n_features[name])
                * ``lags`` (np.array, float), dims: (num_samples, n_lags)
                * ``covariates`` (OrderedDict), named covariates,
                each with features (np.array, float) of dims: (num_samples, n_lags)
                * ``events`` (OrderedDict), events,
                each with features (np.array, float) of dims: (num_samples, n_lags)
                * ``regressors`` (OrderedDict), regressors,
                each with features (np.array, float) of dims: (num_samples, n_lags)
        np.array, float
            Targets to be predicted of same length as each of the model inputs, dims: (num_samples, n_forecasts)
        OrderedDict
            Meta information: static information about the local dataset
        """
        # Convert dataset sample index to valid dataframe positional index
        # - sample index is any index up to len(dataset)
        # - dataframe positional index is given by position of first target in dataframe for given sample index
        df_index = self.sample_index_to_df_index(index)

        # Tabularize - extract features from dataframe at given target index position
        inputs, target = tabularize_univariate_datetime_single_index(self.df, target_index=df_index, **self.kwargs)
        sample, target = self.format_sample(inputs, target)
        return sample, target, self.meta

    def __len__(self):
        """Overrides Parent class method to get data length."""
        return self.length

    def create_sample2index_map(self, df):
        """creates mapping of sample index to df index.
        Create index mapping of sample index to df index
        Filter missing samples and prediction frequency (does not actually drop, but creates indexmapping)
        -- filter samples
            analogous to `self.filter_samples_after_init(self.kwargs["prediction_frequency"])`
        -- drop nan
            analogous to `self.drop_nan_after_init(self.df, self.kwargs["predict_steps"], self.kwargs["config_missing"].drop_missing)
        save the created mapping to sample2index_map
        """
        # Prediction Frequency
        prediction_frequency_mask = self.create_prediction_frequency_filter_mask(
            self, self.config_args["prediction_frequency"]
        )
        # Limit target range due to input lags and number of forecasts
        df_length = len(df)
        max_lags = get_max_num_lags(self.config_args["config_lagged_regressors"], self.config_args["n_lags"])
        n_forecasts = self.config_args["n_forecasts"]
        target_start_end_mask = self.create_target_start_end_mask(
            df_length=df_length, max_lags=max_lags, n_forecasts=n_forecasts
        )

        # TODO Create index mapping of sample index to df index
        # - Filter missing samples (does not actually drop, but creates indexmapping)
        # -- drop nan analogous to `self.drop_nan_after_init(self.df, self.kwargs["predict_steps"], self.kwargs["config_missing"].drop_missing)
        nan_mask = self.create_nan_mask(df)  # vector of all ones, except nans are zeros

        # Combine masks
        mask = np.logical_and(prediction_frequency_mask, target_start_end_mask)
        valid_sample_mask = np.logical_and(mask, nan_mask)
        # Convert boolean valid_sample to list of the positinal index of all true/one entries
        #   e.g. [0,0,1,1,0,1,0] -> [2,3,5]
        index_range = np.arange(0, df_length)
        sample2index_map = index_range[valid_sample_mask]

        num_samples = np.sum(valid_sample_mask)
        assert len(sample2index_map) == num_samples

        return sample2index_map, num_samples

    def sample_index_to_df_index(self, sample_index):
        """Translates a single outer sample to dataframe index"""
        # Will need more sophisticated mapping for GlobalTimeDataset
        return self.sample2index_map[sample_index]

    def drop_nan_init(self, drop_missing):
        """Checks if inputs/targets contain any NaN values and drops them, if user opts to.
        Parameters
        ----------
            drop_missing : bool
                whether to automatically drop missing samples from the data
            predict_steps : int
                number of steps to predict
        """

    def create_target_start_end_mask(self, df_length, max_lags, n_forecasts):
        """Creates a boolean mask for valid targets based on limiting input lags and forecast targets."""
        start_pad = np.zeros(max_lags, dtype=bool)
        valid_targets = np.ones(df_length - max_lags - n_forecasts + 1, dtype=bool)
        end_pad = np.zeros(n_forecasts - 1, dtype=bool)
        target_start_end_mask = np.concatenate((start_pad, valid_targets, end_pad), axis=None)
        return target_start_end_mask

    def create_nan_mask(self, df, predict_steps, drop_missing):
        """Checks if inputs/targets contain any NaN values and drops them, if user opts to.
        Parameters
        ----------
            drop_missing : bool
                whether to automatically drop missing samples from the data
            predict_steps : int
                number of steps to predict
        """
        # TODO: rewrite to return mask instead of filtering df.

        nan_idx = []
        # NaNs in inputs
        for key, data in self.inputs.items():
            if isinstance(data, torch.Tensor):
                nans = torch.where(torch.isnan(data))[0].tolist()
                if len(nans) > 0:
                    nan_idx += nans
            elif isinstance(data, dict):
                for subkey, subdata in data.items():
                    nans = torch.where(torch.isnan(subdata))[0].tolist()
                    if len(nans) > 0:
                        nan_idx += nans

        # NaNs in targets that are not inserted for prediction at the end
        nans = torch.where(torch.isnan(self.targets))[0].tolist()
        if len(nans) > 0:
            for idx in nans:
                if idx not in nan_idx and idx < len(self) - predict_steps:
                    nan_idx.append(idx)

        nan_idx = list(set(nan_idx))
        nan_idx.sort()
        if drop_missing and len(nan_idx) > 0:
            log.warning(f"{len(nan_idx)} samples with missing values were dropped from the data. ")
            for key, data in self.inputs.items():
                if key not in ["time", "lags"]:  # "time_lagged"
                    for name, features in data.items():
                        self.inputs[key][name] = np.delete(self.inputs[key][name], nan_idx, 0)
                else:
                    self.inputs[key] = np.delete(self.inputs[key], nan_idx, 0)
            self.targets = np.delete(self.targets, nan_idx, 0)
            self.length = self.inputs["time"].shape[0]
        if not drop_missing and len(nan_idx) > 0:
            raise ValueError(
                "Inputs/targets with missing values detected. "
                "Please either adjust imputation parameters, or set 'drop_missing' to True to drop those samples."
            )

    @staticmethod
    def _split_nested_dict(inputs):
        """Split nested dict into list of dicts.
        Parameters
        ----------
            inputs : ordered dict
                Nested dict to be split.
        Returns
        -------
            list of dicts
                List of dicts with same keys as inputs.
        """

        def split_dict(inputs, index):
            return {k: v[index] if not isinstance(v, dict) else split_dict(v, index) for k, v in inputs.items()}

        length = next(iter(inputs.values())).shape[0]
        return [split_dict(inputs, i) for i in range(length)]

    def format_sample(self, inputs, targets=None):
        """Convert tabularizes sample to correct formats.
        Parameters
        ----------
            inputs : ordered dict
                Identical to returns from :meth:`tabularize_univariate_datetime`
            targets : np.array, float
                Identical to returns from :meth:`tabularize_univariate_datetime`
        """
        sample_input = OrderedDict({})
        inputs_dtype = {
            "time": torch.float,
            "timestamps": np.datetime64,
            "seasonalities": torch.float,
            "events": torch.float,
            "lags": torch.float,
            "covariates": torch.float,
            "regressors": torch.float,
        }
        targets_dtype = torch.float

        sample_target = torch.from_numpy(targets).type(targets_dtype).unsqueeze(dim=2)

        for key, data in inputs.items():
            if key in self.two_level_inputs:
                sample_input[key] = OrderedDict({})
                for name, features in data.items():
                    if features.dtype != np.float32:
                        features = features.astype(np.float32, copy=False)

                    tensor = torch.from_numpy(features)

                    if tensor.dtype != inputs_dtype[key]:
                        sample_input[key][name] = tensor.to(
                            dtype=inputs_dtype[key]
                        )  # this can probably be removed, but was included in the previous code
                    else:
                        sample_input[key][name] = tensor
            else:
                if key == "timestamps":
                    sample_input[key] = data
                else:
                    sample_input[key] = torch.from_numpy(data).type(inputs_dtype[key])
        sample_input = self._split_nested_dict(sample_input)

        ## Not sure if this needs be done here anymore?
        # Exact timestamps are not needed anymore
        sample_input.pop("timestamps")

        return sample_input, sample_target

    def create_prediction_frequency_filter_mask(
        self,
        prediction_frequency=None,
    ):
        """Filters prediction target index from df based on the forecast frequency setting.
        Parameters
        ----------
            prediction_frequency : int
                periodic interval in which forecasts should be made.
            Note
            ----
            E.g. if prediction_frequency=7, forecasts are only made on every 7th step (once in a week in case of daily
            resolution).

        Returns boolean mask where prediction target start indexes to be included are True, and the rest False.
        """
        if prediction_frequency is None or prediction_frequency == 1:
            return
        # Only the first target timestamp is of interest for filtering
        timestamps = pd.to_datetime([x["timestamps"][0] for x in self.df])  # This may need adjusting
        masks = []
        for key, value in prediction_frequency.items():
            if key == "daily-hour":
                mask = timestamps.hour == value
            elif key == "weekly-day":
                mask = timestamps.dayofweek == value
            elif key == "monthly-day":
                mask = timestamps.day == value
            elif key == "yearly-month":
                mask = timestamps.month == value
            elif key == "hourly-minute":
                mask = timestamps.minute == value
            else:
                raise ValueError(f"Invalid prediction frequency: {key}")
            masks.append(mask)
        mask = np.ones((len(timestamps),), dtype=bool)
        for m in masks:
            mask = mask & m
        return mask


def tabularize_univariate_datetime_single_index(
    df,
    target_index,
    predict_mode=False,
    n_lags=0,
    n_forecasts=1,
    predict_steps=1,
    config_seasonality: Optional[configure.ConfigSeasonality] = None,
    config_events: Optional[configure.ConfigEvents] = None,
    config_country_holidays=None,
    config_lagged_regressors: Optional[configure.ConfigLaggedRegressors] = None,
    config_regressors: Optional[configure.ConfigFutureRegressors] = None,
    config_missing=None,
    config_train=None,
    prediction_frequency=None,
):
    """Create a tabular dataset from univariate timeseries for supervised forecasting.
    Note
    ----
    Data must have no gaps.
    If data contains missing values, they are ignored for the creation of the dataset.
    Parameters
    ----------
        df : pd.DataFrame
            Sequence of observations with original ``ds``, ``y`` and normalized ``t``, ``y_scaled`` columns
        config_seasonality : configure.ConfigSeasonality
            Configuration for seasonalities
        n_lags : int
            Number of lagged values of series to include as model inputs (aka AR-order)
        n_forecasts : int
            Number of steps to forecast into future
        config_events : configure.ConfigEvents
            User specified events, each with their upper, lower windows (int) and regularization
        config_country_holidays : configure.ConfigCountryHolidays
            Configurations (holiday_names, upper, lower windows, regularization) for country specific holidays
        config_lagged_regressors : configure.ConfigLaggedRegressors
            Configurations for lagged regressors
        config_regressors : configure.ConfigFutureRegressors
            Configuration for regressors
        predict_mode : bool
            Chooses the prediction mode
            Options
                * (default) ``False``: Includes target values
                * ``True``: Does not include targets but includes entire dataset as input
    Returns
    -------
        OrderedDict
            Model inputs, each of len(df) but with varying dimensions
            Note
            ----
            Contains the following data:
            Model Inputs
                * ``time`` (np.array, float), dims: (num_samples, 1)
                * ``seasonalities`` (OrderedDict), named seasonalities
                each with features (np.array, float) - dims: (num_samples, n_features[name])
                * ``lags`` (np.array, float), dims: (num_samples, n_lags)
                * ``covariates`` (OrderedDict), named covariates,
                each with features (np.array, float) of dims: (num_samples, n_lags)
                * ``events`` (OrderedDict), events,
                each with features (np.array, float) of dims: (num_samples, n_lags)
                * ``regressors`` (OrderedDict), regressors,
                each with features (np.array, float) of dims: (num_samples, n_lags)
        np.array, float
            Targets to be predicted of same length as each of the model inputs, dims: (num_samples, n_forecasts)
    """
    max_lags = get_max_num_lags(config_lagged_regressors, n_lags)
    # n_samples = len(df) - max_lags + 1 - n_forecasts

    # TODO convert to single sample version

    learning_rate = config_train.learning_rate
    if (
        predict_mode
        or (learning_rate is None)
        or config_lagged_regressors
        or config_country_holidays
        or config_events
        or prediction_frequency
    ):
        n_samples = len(df) - max_lags + 1 - n_forecasts
    else:
        n_samples = 1

    # data is stored in OrderedDict
    inputs = OrderedDict({})

    def _stride_time_features_for_forecasts(x):
        window_size = n_lags + n_forecasts

        if x.ndim == 1:
            shape = (n_samples, window_size)
        else:
            shape = (n_samples, window_size) + x.shape[1:]

        stride = x.strides[0]
        strides = (stride, stride) + x.strides[1:]
        start_index = max_lags - n_lags
        return np.lib.stride_tricks.as_strided(x[start_index:], shape=shape, strides=strides)

    def _stride_future_time_features_for_forecasts(x):
        return np.array([x[max_lags + i : max_lags + i + n_forecasts] for i in range(n_samples)], dtype=x.dtype)

    def _stride_lagged_features(df_col_name, feature_dims):
        # only for case where max_lags > 0
        assert feature_dims >= 1
        series = df.loc[:, df_col_name].values
        # Added dtype=np.float64 to solve the problem with np.isnan for ubuntu test
        return np.array(
            [series[i + max_lags - feature_dims : i + max_lags] for i in range(n_samples)], dtype=np.float32
        )

    def _stride_timestamps_for_forecasts(x):
        # only for case where n_lags > 0
        if x.dtype != np.float64:
            dtype = np.datetime64
        else:
            dtype = np.float64
        return np.array([x[i + max_lags : i + max_lags + n_forecasts] for i in range(n_samples)], dtype=dtype)

    # time is the time at each forecast step
    t = df.loc[:, "t"].values
    if max_lags == 0:
        assert n_forecasts == 1
        time = np.expand_dims(t, 1)
    else:
        time = _stride_time_features_for_forecasts(t)
    inputs["time"] = time  # contains n_lags + n_forecasts

    if prediction_frequency is not None:
        ds = df.loc[:, "ds"].values
        if max_lags == 0:  # is it rather n_lags?
            timestamps = np.expand_dims(ds, 1)
        else:
            timestamps = _stride_timestamps_for_forecasts(ds)
        inputs["timestamps"] = timestamps

    if config_seasonality is not None:
        seasonalities = seasonal_features_from_dates(df, config_seasonality)
        for name, features in seasonalities.items():
            if max_lags == 0:
                seasonalities[name] = np.expand_dims(features, axis=1)
            else:
                # stride into num_forecast at dim=1 for each sample, just like we did with time
                seasonalities[name] = _stride_time_features_for_forecasts(features)
        inputs["seasonalities"] = seasonalities

    if n_lags > 0 and "y" in df.columns:
        inputs["lags"] = _stride_lagged_features(df_col_name="y_scaled", feature_dims=n_lags)

    if config_lagged_regressors is not None and max_lags > 0:
        covariates = OrderedDict({})
        for covar in df.columns:
            if covar in config_lagged_regressors:
                assert config_lagged_regressors[covar].n_lags > 0
                window = config_lagged_regressors[covar].n_lags
                covariates[covar] = _stride_lagged_features(df_col_name=covar, feature_dims=window)
        inputs["covariates"] = covariates

    # get the regressors features
    if config_regressors is not None:
        additive_regressors, multiplicative_regressors = make_regressors_features(df, config_regressors)

        regressors = OrderedDict({})
        if max_lags == 0:
            if additive_regressors is not None:
                regressors["additive"] = np.expand_dims(additive_regressors, axis=1)
            if multiplicative_regressors is not None:
                regressors["multiplicative"] = np.expand_dims(multiplicative_regressors, axis=1)
        else:
            if additive_regressors is not None:
                additive_regressor_feature_windows = []
                # additive_regressor_feature_windows_lagged = []
                for i in range(0, additive_regressors.shape[1]):
                    # stride into num_forecast at dim=1 for each sample, just like we did with time
                    stride = _stride_time_features_for_forecasts(additive_regressors[:, i])
                    additive_regressor_feature_windows.append(stride)
                additive_regressors = np.dstack(additive_regressor_feature_windows)
                regressors["additive"] = additive_regressors

            if multiplicative_regressors is not None:
                multiplicative_regressor_feature_windows = []
                for i in range(0, multiplicative_regressors.shape[1]):
                    stride = _stride_time_features_for_forecasts(multiplicative_regressors[:, i])
                    multiplicative_regressor_feature_windows.append(stride)
                multiplicative_regressors = np.dstack(multiplicative_regressor_feature_windows)
                regressors["multiplicative"] = multiplicative_regressors
        inputs["regressors"] = regressors

    # get the events features
    if config_events is not None or config_country_holidays is not None:
        additive_events, multiplicative_events = make_events_features(df, config_events, config_country_holidays)

        events = OrderedDict({})
        if max_lags == 0:
            if additive_events is not None:
                events["additive"] = np.expand_dims(additive_events, axis=1)
            if multiplicative_events is not None:
                events["multiplicative"] = np.expand_dims(multiplicative_events, axis=1)
        else:
            if additive_events is not None:
                additive_event_feature_windows = []
                for i in range(0, additive_events.shape[1]):
                    # stride into num_forecast at dim=1 for each sample, just like we did with time
                    additive_event_feature_windows.append(_stride_time_features_for_forecasts(additive_events[:, i]))
                additive_events = np.dstack(additive_event_feature_windows)
                events["additive"] = additive_events

            if multiplicative_events is not None:
                multiplicative_event_feature_windows = []
                # multiplicative_event_feature_windows_lagged = []
                for i in range(0, multiplicative_events.shape[1]):
                    # stride into num_forecast at dim=1 for each sample, just like we did with time
                    multiplicative_event_feature_windows.append(
                        _stride_time_features_for_forecasts(multiplicative_events[:, i])
                    )
                multiplicative_events = np.dstack(multiplicative_event_feature_windows)
                events["multiplicative"] = multiplicative_events
        inputs["events"] = events

    if predict_mode:
        targets = np.empty_like(time[:, n_lags:])
        targets = np.nan_to_num(targets)
    else:
        targets = _stride_future_time_features_for_forecasts(df["y_scaled"].values)

    tabularized_input_shapes_str = ""
    for key, value in inputs.items():
        if key in [
            "seasonalities",
            "covariates",
            "events",
            "regressors",
        ]:
            for name, period_features in value.items():
                tabularized_input_shapes_str += f"    {name} {key} {period_features}\n"
        else:
            tabularized_input_shapes_str += f"    {key} {value.shape} \n"
    log.debug(f"Tabularized inputs shapes: \n{tabularized_input_shapes_str}")

    return inputs, targets


def fourier_series(dates, period, series_order):
    """Provides Fourier series components with the specified frequency and order.
    Note
    ----
    Identical to OG Prophet.
    Parameters
    ----------
        dates : pd.Series
            Containing timestamps
        period : float
            Number of days of the period
        series_order : int
            Number of fourier components
    Returns
    -------
        np.array
            Matrix with seasonality features
    """
    # convert to days since epoch
    t = np.array((dates - datetime(1970, 1, 1)).dt.total_seconds().astype(np.float32)) / (3600 * 24.0)
    return fourier_series_t(t, period, series_order)


def fourier_series_t(t, period, series_order):
    """Provides Fourier series components with the specified frequency and order.
    Note
    ----
    This function is identical to Meta AI's Prophet Library
    Parameters
    ----------
        t : pd.Series, float
            Containing time as floating point number of days
        period : float
            Number of days of the period
        series_order : int
            Number of fourier components
    Returns
    -------
        np.array
            Matrix with seasonality features
    """
    features = np.column_stack(
        [fun((2.0 * (i + 1) * np.pi * t / period)) for i in range(series_order) for fun in (np.sin, np.cos)]
    )
    return features


def make_country_specific_holidays_df(year_list, country):
    """
    Make dataframe of country specific holidays for given years and countries
    Parameters
    ----------
        year_list : list
            List of years
        country : str, list
            List of country names
    Returns
    -------
        pd.DataFrame
            Containing country specific holidays df with columns 'ds' and 'holiday'
    """
    # iterate over countries and get holidays for each country
    # convert to list if not already
    if isinstance(country, str):
        country = [country]
    country_specific_holidays = {}
    for single_country in country:
        single_country_specific_holidays = get_country_holidays(single_country, year_list)
        # only add holiday if it is not already in the dict
        country_specific_holidays.update(single_country_specific_holidays)
    country_specific_holidays_dict = defaultdict(list)
    for date, holiday in country_specific_holidays.items():
        country_specific_holidays_dict[holiday].append(pd.to_datetime(date))
    return country_specific_holidays_dict


def _create_event_offset_features(event, config, feature, additive_events, multiplicative_events):
    """
    Create event offset features for the given event, config and feature
    Parameters
    ----------
        event : str
            Name of the event
        config : configure.ConfigEvents
            User specified events, holidays, and country specific holidays
        feature : pd.Series
            Feature for the event
        additive_events : pd.DataFrame
            Dataframe of additive events
        multiplicative_events : pd.DataFrame
            Dataframe of multiplicative events
    Returns
    -------
        tuple
            Tuple of additive_events and multiplicative_events
    """
    lw = config.lower_window
    uw = config.upper_window
    mode = config.mode
    for offset in range(lw, uw + 1):
        key = utils.create_event_names_for_offsets(event, offset)
        offset_feature = feature.shift(periods=offset, fill_value=0.0)
        if mode == "additive":
            additive_events[key] = offset_feature
        else:
            multiplicative_events[key] = offset_feature


def make_events_features(df, config_events: Optional[configure.ConfigEvents] = None, config_country_holidays=None):
    """
    Construct arrays of all event features
    Parameters
    ----------
        df : pd.DataFrame
            Dataframe with all values including the user specified events (provided by user)
        config_events : configure.ConfigEvents
            User specified events, each with their upper, lower windows (int), regularization
        config_country_holidays : configure.ConfigCountryHolidays
            Configurations (holiday_names, upper, lower windows, regularization) for country specific holidays
    Returns
    -------
        np.array
            All additive event features (both user specified and country specific)
        np.array
            All multiplicative event features (both user specified and country specific)
    """
    df = df.reset_index(drop=True)
    additive_events = pd.DataFrame()
    multiplicative_events = pd.DataFrame()

    # create all user specified events
    if config_events is not None:
        for event, configs in config_events.items():
            feature = df[event]
            _create_event_offset_features(event, configs, feature, additive_events, multiplicative_events)

    # create all country specific holidays
    if config_country_holidays is not None:
        year_list = list({x.year for x in df.ds})
        country_holidays_dict = make_country_specific_holidays_df(year_list, config_country_holidays.country)
        for holiday in config_country_holidays.holiday_names:
            feature = pd.Series([0.0] * df.shape[0])
            if holiday in country_holidays_dict.keys():
                dates = country_holidays_dict[holiday]
                feature[df.ds.isin(dates)] = 1.0
            _create_event_offset_features(
                holiday, config_country_holidays, feature, additive_events, multiplicative_events
            )

    # Make sure column order is consistent
    if not additive_events.empty:
        additive_events = additive_events[sorted(additive_events.columns.tolist())]
        additive_events = additive_events.values
    else:
        additive_events = None
    if not multiplicative_events.empty:
        multiplicative_events = multiplicative_events[sorted(multiplicative_events.columns.tolist())]
        multiplicative_events = multiplicative_events.values
    else:
        multiplicative_events = None

    return additive_events, multiplicative_events


def make_regressors_features(df, config_regressors):
    """Construct arrays of all scalar regressor features
    Parameters
    ----------
        df : pd.DataFrame
            Dataframe with all values including the user specified regressors
        config_regressors : configure.ConfigFutureRegressors
            User specified regressors config
    Returns
    -------
        np.array
            All additive regressor features
        np.array
            All multiplicative regressor features
    """
    additive_regressors = pd.DataFrame()
    multiplicative_regressors = pd.DataFrame()

    for reg in df.columns:
        if reg in config_regressors:
            mode = config_regressors[reg].mode
            if mode == "additive":
                additive_regressors[reg] = df[reg]
            else:
                multiplicative_regressors[reg] = df[reg]

    if not additive_regressors.empty:
        additive_regressors = additive_regressors[sorted(additive_regressors.columns.tolist())]
        additive_regressors = additive_regressors.values
    else:
        additive_regressors = None
    if not multiplicative_regressors.empty:
        multiplicative_regressors = multiplicative_regressors[sorted(multiplicative_regressors.columns.tolist())]
        multiplicative_regressors = multiplicative_regressors.values
    else:
        multiplicative_regressors = None

    return additive_regressors, multiplicative_regressors


def seasonal_features_from_dates(df, config_seasonality: configure.ConfigSeasonality):
    """Dataframe with seasonality features.
    Includes seasonality features, holiday features, and added regressors.
    Parameters
    ----------
        df : pd.DataFrame
            Dataframe with all values
        config_seasonality : configure.ConfigSeasonality
            Configuration for seasonalities
    Returns
    -------
        OrderedDict
            Dictionary with keys for each period name containing an np.array
            with the respective regression features. each with dims: (len(dates), 2*fourier_order)
    """
    dates = df["ds"]
    assert len(dates.shape) == 1
    seasonalities = OrderedDict({})
    # Seasonality features
    for name, period in config_seasonality.periods.items():
        if period.resolution > 0:
            if config_seasonality.computation == "fourier":
                features = fourier_series(
                    dates=dates,
                    period=period.period,
                    series_order=period.resolution,
                )
            else:
                raise NotImplementedError
            if period.condition_name is not None:
                features = features * df[period.condition_name].values[:, np.newaxis]
            seasonalities[name] = features
    return seasonalities
