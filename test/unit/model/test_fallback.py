# SPDX-FileCopyrightText: 2017-2023 Contributors to the OpenSTEF project <korte.termijn.prognoses@alliander.com> # noqa E501>
#
# SPDX-License-Identifier: MPL-2.0

import unittest
from test.unit.utils.base import BaseTestCase
from test.unit.utils.data import TestData

import numpy as np

from openstef.model.fallback import generate_fallback
from openstef.enums import FallbackStrategy


class TestFallback(BaseTestCase):
    def test_generate_fallback_happy_flow(self):
        """Test happy flow of fallback forecast"""
        expected_forecast = TestData.load("fallback_forecast.csv")
        load = TestData.load("fallback_load.csv")
        forc_section = TestData.load("fallback_index.csv")

        fallback_forecast = generate_fallback(
            forecast_input=forc_section,
            load=load,
            fallback_strategy=FallbackStrategy.EXTREME_DAY,
        )

        self.assertDataframeEqual(
            fallback_forecast.sort_index(),
            expected_forecast.sort_index(),
        )

    def test_generate_fallback_empty_load(self):
        """Test if exception is raised if load is empty"""
        load = TestData.load("fallback_load.csv")
        load *= np.nan
        forc_section = TestData.load("fallback_index.csv")
        self.assertRaises(
            ValueError,
            generate_fallback,
            forecast_input=forc_section.index,
            load=load,
        )

    def test_generate_fallback_unimplemented_input(self):
        load = TestData.load("fallback_load.csv")
        forc_section = TestData.load("fallback_index.csv")
        self.assertRaises(
            NotImplementedError,
            generate_fallback,
            forecast_input=forc_section,
            load=load,
            fallback_strategy="SomeWeirdNotImplementedStrategy",
        )

    def test_generate_fallback_raise_error(self):
        """Test if exception is raised if load is empty"""
        load = TestData.load("fallback_load.csv")
        load *= np.nan
        forc_section = TestData.load("fallback_index.csv")
        self.assertRaises(
            ValueError,
            generate_fallback,
            forecast_input=forc_section.index,
            load=load,
            fallback_strategy=FallbackStrategy.RAISE_ERROR,
        )


if __name__ == "__main__":
    unittest.main()
