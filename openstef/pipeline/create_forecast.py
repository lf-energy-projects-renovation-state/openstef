# SPDX-FileCopyrightText: 2017-2023 Contributors to the OpenSTEF project <korte.termijn.prognoses@alliander.com> # noqa E501>
#
# SPDX-License-Identifier: MPL-2.0

import pandas as pd

from openstef.data_classes.model_specifications import ModelSpecificationDataClass
from openstef.data_classes.prediction_job import PredictionJobDataClass
from openstef.feature_engineering.feature_applicator import (
    OperationalPredictFeatureApplicator,
)
from openstef.logging.logger_factory import get_logger
from openstef.model.confidence_interval_applicator import ConfidenceIntervalApplicator
from openstef.model.fallback import generate_fallback
from openstef.model.regressors.regressor import OpenstfRegressor
from openstef.model.serializer import MLflowSerializer
from openstef.pipeline.utils import generate_forecast_datetime_range
from openstef.postprocessing.postprocessing import (
    add_prediction_job_properties_to_forecast,
    sort_quantiles,
)
from openstef.validation import validation
from openstef.enums import FallbackStrategy


def create_forecast_pipeline(
    pj: PredictionJobDataClass,
    input_data: pd.DataFrame,
    mlflow_tracking_uri: str,
) -> pd.DataFrame:
    """Create forecast pipeline.

    This is the top-level pipeline which included loading the most recent model for
    the given prediction job.

    Expected prediction job keys: "id",

    Args:
        pj: Prediction job
        input_data: Training input data (without features)
        mlflow_tracking_uri: MlFlow tracking URI

    Returns:
        DataFrame with the forecast

    Raises:
        InputDataOngoingFlatlinerError: When all recent load measurements are constant.
        LookupError: When no model is found for the given prediction job in MLflow.

    """
    prediction_model_pid = pj["id"]
    # Use the alternative forecast model if it's specify in the pj
    if pj.alternative_forecast_model_pid:
        prediction_model_pid = pj.alternative_forecast_model_pid
    # Load most recent model for the given pid
    model, model_specs = MLflowSerializer(
        mlflow_tracking_uri=mlflow_tracking_uri
    ).load_model(experiment_name=str(prediction_model_pid), model_run_id=pj.get("model_run_id"))
    return create_forecast_pipeline_core(pj, input_data, model, model_specs)


def create_forecast_pipeline_core(
    pj: PredictionJobDataClass,
    input_data: pd.DataFrame,
    model: OpenstfRegressor,
    model_specs: ModelSpecificationDataClass,
) -> pd.DataFrame:
    """Create forecast pipeline (core).

    Computes the forecasts and confidence intervals given a prediction job and input data.
    This pipeline has no database or persisitent storage dependencies.

    Expected prediction job keys: "resolution_minutes", "id", "type",
        "name", "quantiles"

    Args:
        pj: Prediction job.
        input_data: Input data for the prediction.
        model: Model to use for this prediction.
        model_specs: Model specifications.

    Returns:
        Forecast

    Raises:
        InputDataOngoingFlatlinerError: When all recent load measurements are constant.

    """
    logger = get_logger(__name__)

    fallback_strategy = pj.get("fallback_strategy", FallbackStrategy.EXTREME_DAY)

    # Validate and clean data
    validated_data = validation.validate(
        pj["id"],
        input_data,
        pj["flatliner_threshold_minutes"],
        pj["resolution_minutes"],
        detect_non_zero_flatliner=pj["detect_non_zero_flatliner"],
    )

    # Custom data prep or legacy behavior
    if pj.data_prep_class:
        data_prep_class, data_prep_args = pj.data_prep_class.load()
        forecast_input_data, data_with_features = data_prep_class(
            pj=pj,
            model_specs=model_specs,
            model=model,
            **data_prep_args,
        ).prepare_forecast_data(validated_data)
    else:
        # Add features
        data_with_features = OperationalPredictFeatureApplicator(
            horizons=[pj["resolution_minutes"] / 60.0],
            feature_names=model.feature_names,
            feature_modules=model_specs.feature_modules,
        ).add_features(validated_data, pj=pj)

        # Prep forecast input by selecting only the forecast datetime interval (this is much smaller than the input range)
        # Also drop the load column
        forecast_start, forecast_end = generate_forecast_datetime_range(
            data_with_features
        )
        forecast_input_data = data_with_features[forecast_start:forecast_end].drop(
            columns="load"
        )

    # Check if sufficient data is left after cleaning
    if not validation.is_data_sufficient(
        data_with_features,
        pj["completeness_threshold"],
        pj["minimal_table_length"],
        model,
    ):
        logger.warning(
            "Using fallback forecast",
            forecast_type="fallback",
            pid=pj["id"],
            fallback_strategy=fallback_strategy,
        )
        forecast = generate_fallback(data_with_features, input_data[["load"]])

    else:
        # Predict
        model_forecast = model.predict(forecast_input_data)
        forecast = pd.DataFrame(
            index=forecast_input_data.index, data={"forecast": model_forecast}
        )

    # Add confidence
    forecast = ConfidenceIntervalApplicator(
        model, forecast_input_data
    ).add_confidence_interval(forecast, pj)

    # Sort quantiles - prevents crossing and is statistically sound
    forecast = sort_quantiles(forecast)

    # Prepare for output
    forecast = add_prediction_job_properties_to_forecast(
        pj,
        forecast,
        algorithm_type=str(model.path),
    )

    return forecast
