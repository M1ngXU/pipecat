#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

from typing import Any, Dict, List

from pipecat.services.function_calling.function_schema import FunctionSchema


class AnthropicFunctionAdapter:
    @staticmethod
    def to_provider_format(function_schema: FunctionSchema) -> Dict[str, Any]:
        """Converts the function schema to Anthropic's function-calling format.

        :return: Anthropic formatted function call definition.
        """
        return {
            "name": function_schema.name,
            "description": function_schema.description,
            "input_schema": {
                "type": "object",
                "properties": function_schema.properties,
                "required": function_schema.required,
            },
        }