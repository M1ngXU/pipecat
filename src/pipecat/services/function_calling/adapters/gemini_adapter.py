#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

from typing import Any, Dict

from pipecat.services.function_calling.function_schema import FunctionSchema


class GeminiFunctionAdapter:
    @staticmethod
    def to_provider_format(function_schema: FunctionSchema) -> Dict[str, Any]:
        """Converts the function schema to Gemini's function-calling format.

        :return: Gemini formatted function call definition.
        """
        return function_schema.to_default_dict()

        # TODO: this is going to be used when converting a list of FunctionSchema
        # return {
        #        "function_declarations": [
        #            function_schema.to_default_dict()
        #        ]
        #    }
