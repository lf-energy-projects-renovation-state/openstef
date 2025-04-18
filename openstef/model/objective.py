# SPDX-FileCopyrightText: 2017-2023 Contributors to the OpenSTEF project <korte.termijn.prognoses@alliander.com> # noqa E501>
#
# SPDX-License-Identifier: MPL-2.0
import copy
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import optuna
import pandas as pd
from lightgbm import early_stopping
from xgboost.callback import EarlyStopping

from openstef.enums import ModelType
from openstef.metrics import metrics
from openstef.metrics.reporter import Report, Reporter
from openstef.model.regressors.regressor import OpenstfRegressor
from openstef.model.standard_deviation_generator import StandardDeviationGenerator
from openstef.model_selection.model_selection import split_data_train_validation_test

EARLY_STOPPING_ROUNDS: int = 10
TEST_FRACTION: float = 0.15
VALIDATION_FRACTION: float = 0.15
# See https://xgboost.readthedocs.io/en/latest/parameter.html for all possibilities
EVAL_METRIC: str = "mae"

# https://optuna.readthedocs.io/en/stable/faq.html#objective-func-additional-args


class RegressorObjective:
    """Regressor optuna objective function.

    Use any of the derived classes for optimization using an optuna study.
    The constructor is used to set the "input_data", specify the splitting function
    and its arguments and optionally add some configuration.
    Next the instance will be called by he optuna study during optimization.

    Example usage:

    .. code-block:: py

        # initialize a (derived class) objective function
        objective = XGBRegressorObjective(input_data, test_fraction)
        # use the objective function
        study.optimize(objective)

    """

    def __init__(
        self,
        model: OpenstfRegressor,
        input_data: pd.DataFrame,
        split_func: Optional[Callable] = None,
        split_args: Optional[dict[str, Any]] = None,
        test_fraction=TEST_FRACTION,
        validation_fraction=VALIDATION_FRACTION,
        eval_metric=EVAL_METRIC,
        verbose=False,
    ):
        self.input_data = input_data
        self.train_data = None
        self.validation_data = None
        self.test_data = None
        self.model = model
        self.start_time = datetime.now(timezone.utc)
        self.test_fraction = test_fraction
        self.validation_fraction = validation_fraction
        self.eval_metric = eval_metric
        self.eval_metric_function = metrics.get_eval_metric_function(eval_metric)
        self.verbose = verbose
        # Should be set on a derived classes
        self.model_type = None
        self.track_trials = {}

        # split function and arguments
        self.split_func = split_func
        self.split_args = split_args

        # default behavior for splitting
        if self.split_func is None:
            self.split_func = split_data_train_validation_test
            self.split_args = None

    def __call__(
        self,
        trial: optuna.trial.FrozenTrial,
    ) -> float:
        """Optuna objective function.

        Args: trial

        Returns:
            Mean absolute error for this trial.

        """
        # Perform data preprocessing
        split_args = self.split_args
        if split_args is None:
            split_args = {
                "stratification_min_max": True,
                "back_test": True,
            }
        (
            self.train_data,
            self.validation_data,
            self.test_data,
            self.operational_score_data,
        ) = self.split_func(
            self.input_data,
            test_fraction=self.test_fraction,
            validation_fraction=self.validation_fraction,
            **split_args,
        )

        # Test if first column is "load" and last column is "horizon"
        if (
            self.train_data.columns[0] != "load"
            or self.train_data.columns[-1] != "horizon"
        ):
            raise RuntimeError(
                "Column order in train input data not as expected, "
                "could not train a model!"
            )

        # Split in x, y data (x are the features, y is the load)
        train_x, train_y = self.train_data.iloc[:, 1:-1], self.train_data.iloc[:, 0]
        valid_x, valid_y = (
            self.validation_data.iloc[:, 1:-1],
            self.validation_data.iloc[:, 0],
        )
        test_x, test_y = self.test_data.iloc[:, 1:-1], self.test_data.iloc[:, 0]

        # Configure evals for early stopping
        eval_set = [(train_x, train_y), (valid_x, valid_y)]

        # Get the parameters used in this trial
        hyper_params = self.get_params(trial)

        # Insert parameters into model
        self.model.set_params(**hyper_params)

        callbacks = []

        # Create the early stopping callback
        early_stopping_callback = self.get_early_stopping_callback()
        if early_stopping_callback is not None:
            callbacks.append(early_stopping_callback)

        # Create the specific pruning callback
        pruning_callback = self.get_pruning_callback(trial)
        if pruning_callback is not None:
            callbacks.append(pruning_callback)

        # Pass verbose argument to fit call if model is not LGB
        fit_kwargs = {}
        if self.model_type not in [ModelType.XGB, ModelType.LGB]:
            fit_kwargs["early_stopping_rounds"] = EARLY_STOPPING_ROUNDS
        elif self.model_type != ModelType.LGB:
            fit_kwargs["verbose"] = self.verbose

        # validation_0 and validation_1 are available
        self.model.fit(
            train_x,
            train_y,
            eval_set=eval_set,
            eval_metric=self.eval_metric,
            callbacks=callbacks,
            **fit_kwargs,
        )

        self.model.feature_importance_dataframe = self.model.get_feature_importance()

        # Do confidence interval determination
        self.model = StandardDeviationGenerator(
            self.validation_data
        ).generate_standard_deviation_data(self.model)

        forecast_y = self.model.predict(test_x)
        score = self.eval_metric_function(test_y, forecast_y)

        # Convert float32 to float because float32 is not JSON serializable
        self.track_trials[f" trial: {trial.number}"] = {
            "score": float(score),
            "params": hyper_params,
        }
        trial.set_user_attr(key="model", value=copy.deepcopy(self.model))
        return score

    def get_params(self, trial: optuna.trial.FrozenTrial) -> dict:
        """Get parameters for objective without model specific get_params function.

        Args: trial

        Returns:
            Dictionary with hyperparameter name as key and hyperparamer value as value.

        """
        default_params = {
            "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.5),
            "alpha": trial.suggest_float("alpha", 0, 1.0),
            "lambda": trial.suggest_float("lambda", 1e-8, 1.0),
            "subsample": trial.suggest_float("subsample", 0.4, 1.0),
            "min_child_weight": trial.suggest_int("min_child_weight", 1, 16),
            "max_depth": trial.suggest_int("max_depth", 3, 10),
            "colsample_bytree": trial.suggest_float("colsample_bytree", 0.5, 1.0),
            "max_delta_step": trial.suggest_int("max_delta_step", 0, 10),
        }

        # Compare the list to the default parameter space
        model_parameters = self.model.get_params()
        keys = [x for x in model_parameters.keys() if x in default_params.keys()]
        # create a dictionary with the matching parameters
        params = {parameter: default_params[parameter] for parameter in keys}

        return params

    def get_pruning_callback(self, trial: optuna.trial.FrozenTrial):
        return None

    def get_early_stopping_callback(self):
        return None

    def get_trial_track(self) -> dict:
        """Get a dictionary of al trials.

        Returns:
            Dict with al trials and it's parameters

        """
        return self.track_trials

    def create_report(self, model: OpenstfRegressor) -> Report:
        """Generate a report from the data available inside the objective function.

        Args:
            model: OpenstfRegressor, model to create a report on

        Returns:
            Report about the model

        """
        # Report about the training process
        reporter = Reporter(self.train_data, self.validation_data, self.test_data)
        report = reporter.generate_report(model)

        return report

    @classmethod
    def get_default_values(cls) -> dict:
        return {
            "learning_rate": 0.3,
            "alpha": 0.0,
            "lambda": 1.0,
            "subsample": 1.0,
            "min_child_weight": 1,
            "max_depth": 6,
            "colsample_bytree": 1,
            "max_delta_step": 0,
        }


class XGBRegressorObjective(RegressorObjective):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.model_type = ModelType.XGB

    # extend the parameters with the model specific ones per implementation
    def get_params(self, trial: optuna.trial.FrozenTrial) -> dict:
        """Get parameters for XGB Regressor Objective with objective specific parameters.

        Args: trial

        Returns:
            Dictionary with hyperparameter name as key and hyperparamer value as value.

        """
        # Filtered default parameters
        model_params = super().get_params(trial)

        # XGB specific parameters
        params = {
            "gamma": trial.suggest_float("gamma", 0.0, 1.0),
            "booster": trial.suggest_categorical("booster", ["gbtree", "dart"]),
        }
        return {**model_params, **params}

    def get_pruning_callback(self, trial: optuna.trial.FrozenTrial):
        return optuna.integration.XGBoostPruningCallback(
            trial, observation_key=f"validation_1-{self.eval_metric}"
        )

    def get_early_stopping_callback(self):
        return EarlyStopping(
            rounds=EARLY_STOPPING_ROUNDS,
            metric_name=self.eval_metric,
            data_name=f"validation_1",
            maximize=False,
            save_best=True,
        )

    @classmethod
    def get_default_values(cls) -> dict:
        default_parameter_values = super().get_default_values()
        default_parameter_values.update({"gamma": 0.0, "booster": "gbtree"})
        return default_parameter_values


class LGBRegressorObjective(RegressorObjective):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.model_type = ModelType.LGB

    def get_params(self, trial: optuna.trial.FrozenTrial) -> dict:
        """Get parameters for LGB Regressor Objective with objective specific parameters.

        Args: trial

        Returns:
            Dictionary with hyperparameter name as key and hyperparamer value as value.

        """
        # Filtered default parameters
        model_params = super().get_params(trial)

        # LGB specific parameters
        params = {
            "num_leaves": trial.suggest_int("num_leaves", 16, 62),
            "boosting_type": trial.suggest_categorical(
                "boosting_type", ["gbdt", "dart", "rf"]
            ),
            "tree_learner": trial.suggest_categorical(
                "tree_learner", ["serial", "feature", "data", "voting"]
            ),
            "n_estimators": trial.suggest_int("n_estimators", 50, 150),
            "min_split_gain": trial.suggest_float("min_split_gain", 1e-8, 1),
            "subsample_freq": trial.suggest_int("subsample_freq", 1, 10),
        }
        return {**model_params, **params}

    def get_pruning_callback(self, trial: optuna.trial.FrozenTrial):
        metric = self.eval_metric
        if metric == "mae":
            metric = "l1"
        return optuna.integration.LightGBMPruningCallback(
            trial, metric=metric, valid_name="valid_1"
        )

    def get_early_stopping_callback(self):
        return early_stopping(
            stopping_rounds=EARLY_STOPPING_ROUNDS, verbose=self.verbose
        )


class XGBQuantileRegressorObjective(RegressorObjective):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.model_type = ModelType.XGB_QUANTILE

    def get_params(self, trial: optuna.trial.FrozenTrial) -> dict:
        """Get parameters for XGBQuantile Regressor Objective with objective specific parameters.

        Args: trial

        Returns:
            Dictionary with hyperparameter name as key and hyperparamer value as value.

        """
        # Filtered default parameters
        model_params = super().get_params(trial)

        # XGB specific parameters
        params = {
            "gamma": trial.suggest_float("gamma", 1e-8, 1.0),
        }
        return {**model_params, **params}

    def get_pruning_callback(self, trial: optuna.trial.FrozenTrial):
        return optuna.integration.XGBoostPruningCallback(
            trial, observation_key=f"validation_1-{self.eval_metric}"
        )


class XGBMultioutputQuantileRegressorObjective(RegressorObjective):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.model_type = ModelType.XGB_QUANTILE

    def get_params(self, trial: optuna.trial.FrozenTrial) -> dict:
        """Get parameters for XGB Multioutput Quantile Regressor Objective with objective specific parameters.

        Args: trial

        Returns:
            Dictionary with hyperparameter name as key and hyperparamer value as value.

        """
        # Filtered default parameters
        model_params = super().get_params(trial)

        # XGB specific parameters
        params = {
            "gamma": trial.suggest_float("gamma", 1e-8, 1.0),
            "arctan_smoothing": trial.suggest_float("arctan_smoothing", 0.025, 0.15),
        }
        return {**model_params, **params}

    def get_pruning_callback(self, trial: optuna.trial.FrozenTrial):
        return optuna.integration.XGBoostPruningCallback(
            trial, observation_key=f"validation_1-{self.eval_metric}"
        )


class LinearRegressorObjective(RegressorObjective):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.model_type = ModelType.LINEAR

    def get_params(self, trial: optuna.trial.FrozenTrial) -> dict:
        """Get parameters for Linear Regressor Objective with objective specific parameters.

        Args: trial

        Returns:
            Dictionary with hyperparameter name as key and hyperparamer value as value.

        """
        # Imputation strategy
        params = {
            "imputation_strategy": trial.suggest_categorical(
                "imputation_strategy", ["mean", "median", "most_frequent"]
            ),
        }
        return params


class ARIMARegressorObjective(RegressorObjective):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.model_type = ModelType.ARIMA

    def get_params(self, trial: optuna.trial.FrozenTrial) -> dict:
        """Get parameters for ARIMA Regressor Objective with objective specific parameters.

        Temporary, it seems strange to use optuna for ARIMA models,
        it is usually done via statistical analysis and heuristics.

        Args: trial

        Returns:
            Dictionary with hyperparameter name as key and hyperparamer value as value.

        """
        # Imputation strategy
        params = {
            "trend": trial.suggest_categorical("trend", ["n", "c", "t", "ct"]),
        }
        return params
