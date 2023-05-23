# pylint: disable=invalid-name, line-too-long
"""Functions for extracting dataset features and labels"""
from importlib import import_module

import numpy as np
from scipy import stats
from sklearn.model_selection import StratifiedKFold

from omegaconf import OmegaConf, DictConfig, open_dict


def data_factory(cfg: DictConfig):
    """
    Model-agnostic data factory.
    1. Loads 'cfg.dataset.name' dataset (requires src.datasets.{cfg.dataset.name}.load_data(cfg) to be defined)
    2. Selects tuning or experiment portion if cfg.dataset.tuning_holdout is True
    3. Processes the data in common_processor, or some custom processor if
        cfg.dataset.custom_processor is True and src.datasets.{cfg.dataset.name}.get_processor(data, cfg) is defined
    4. Save data_info returned by processor in cfg.dataset.data_info, and return processed data

    Processed data is a dictionary with
    {
        "main": cfg.dataset.name dataset,
        "{additional_datasets}": additional test datsets (optional),
    }
    data_info is a dictionary with
    {
        "main": main dataset info (depends on the processor),
        "{additional_datasets}": additional test datsets info (optional),
    }
    """
    # load dataset
    try:
        dataset_module = import_module(f"src.datasets.{cfg.dataset.name}")
    except ModuleNotFoundError as e:
        raise ModuleNotFoundError(
            f"No module named '{cfg.dataset.name}' \
                                  found in 'src.datasets'. Check if dataset name \
                                  in config file and its module name are the same"
        ) from e

    try:
        ts_data, labels = dataset_module.load_data(cfg)
    except AttributeError as e:
        raise AttributeError(
            f"'src.datasets.{cfg.dataset.name}' has no function\
                             'load_data'. Is the function misnamed/not defined?"
        ) from e

    # select tuning holdout (if needed)
    if "tuning_holdout" in cfg.dataset and cfg.dataset.tuning_holdout:
        assert (
            cfg.exp.tuning_split is not None
        ), "you must specify 'exp.tuning_split' if \
                 'exp.tuning_holdout' is set to True"
        assert isinstance(cfg.exp.tuning_split, int)

        skf = StratifiedKFold(
            n_splits=cfg.dataset.tuning_split, shuffle=True, random_state=42
        )
        CV_folds = list(skf.split(ts_data, labels))
        train_index, test_index = CV_folds[0]
        if cfg.mode.name == "tune":
            ts_data = ts_data[test_index]
            labels = labels[test_index]
        else:
            ts_data = ts_data[train_index]
            labels = labels[train_index]

    # process data
    if "custom_processor" not in cfg.dataset or not cfg.dataset.custom_processor:
        processor = common_processor
    else:
        try:
            dataset_module = import_module(f"src.datasets.{cfg.dataset.name}")
        except ModuleNotFoundError as e:
            raise ModuleNotFoundError(
                f"No module named '{cfg.dataset.name}' \
                                    found in 'src.datasets'. Check if dataset name \
                                    in config file and its module name are the same"
            ) from e

        try:
            get_processor = dataset_module.get_processor
        except AttributeError as e:
            raise AttributeError(
                f"'src.datasets.{cfg.dataset.name}' has no function\
                                'get_processor'. Is the function misnamed/not defined?"
            ) from e

        processor = get_processor()

    data = {}
    data_info = {}
    data["main"], data_info["main"] = processor((ts_data, labels), cfg)

    # TODO: add additional test datasets

    with open_dict(cfg):
        cfg.dataset.data_info = data_info

    return data


def common_processor(data, cfg: DictConfig):
    """
    Return processed data and data_info based on config

    "TS" data is z-scored over time if cfg.model.zscore is True
    "FNC" is obtained using Pearson correlation coefficients

    Returns (data, data_info) tuple.
    Data is a dict with
    {
        "TS": TS-data of shape [subjects, time, components],
        "labels": labels
    } if cfg.model.data_type is TS or undefined;
    {
        "FNC": FNC-data of shape [subjects, components, components],
        "labels": labels
    } if cfg.model.data_type is FNC;
    {
        "FNC": FNC-data of shape [subjects, flattened_upper_FNC_triangle],
        "labels": labels
    } if cfg.model.data_type is tri-FNC;
    {
        "TS": TS-data of shape [subjects, time, components],
        "FNC": FNC-data of shape [subjects, components, components],
        "labels": labels
    } if cfg.model.data_type is TS-FNC;

    data_info is a DictConfig with
    {
        "data_shape": TS or FNC data shape, or a dict
            {
                "TS": shape,
                "FNC": shape,
            }, if cfg.model.data_type is TS-FNC
        "n_classes": n_classes,
    }
    )
    """

    ts_data, labels = data
    n_classes = np.unique(labels).shape[0]

    # z-score the data over time
    if cfg.dataset.zscore:
        ts_data = stats.zscore(ts_data, axis=1)

    # derive FNC data, if needed
    if "data_type" not in cfg.model or cfg.model.data_type == "TS":
        data = {"TS": ts_data, "labels": labels}
        data_shape = ts_data.shape

    elif cfg.model.data_type in ["FNC", "tri-FNC", "TS-FNC"]:
        pearson = np.zeros((ts_data.shape[0], ts_data.shape[2], ts_data.shape[2]))
        for i in range(ts_data.shape[0]):
            pearson[i, :, :] = np.corrcoef(ts_data[i, :, :], rowvar=False)

        if cfg.model.data_type == "FNC":
            data = {"FNC": pearson, "labels": labels}
            data_shape = pearson.shape
        elif cfg.model.data_type == "tri-FNC":
            tril_inx = np.tril_indices(pearson.shape[1])
            triangle = np.zeros((pearson.shape[0], tril_inx[0].shape[0]))
            for i in range(triangle.shape[0]):
                triangle[i] = pearson[i][tril_inx]
            data = {"FNC": triangle, "labels": labels}
            data_shape = triangle.shape
        elif cfg.model.data_type == "TS-FNC":
            data = {"TS": ts_data, "FNC": pearson, "labels": labels}
            data_shape = {"TS": ts_data.shape, "FNC": pearson.shape}

    data_info = OmegaConf.create(
        {
            "data_shape": data_shape,
            "n_classes": n_classes,
        }
    )

    return data, data_info


def data_postfactory(cfg: DictConfig, model_cfg: DictConfig, original_data):
    """
    Post-process the raw dataset according to model_cfg if cfg.model.require_data_postproc is True
    """
    if "require_data_postproc" not in cfg.model or not cfg.model.require_data_postproc:
        data = original_data
    else:
        try:
            model_module = import_module(f"src.models.{cfg.model.name}")
        except ModuleNotFoundError as e:
            raise ModuleNotFoundError(
                f"No module named '{cfg.model.name}' \
                                    found in 'src.models'. Check if model name \
                                    in config file and its module name are the same"
            ) from e

        try:
            data_postproc = model_module.data_postproc
        except AttributeError as e:
            raise AttributeError(
                f"'src.models.{cfg.model.name}' has no function\
                                'data_postproc'. Is the function misnamed/not defined?"
            ) from e

        data = data_postproc(cfg, model_cfg, original_data)

    return data