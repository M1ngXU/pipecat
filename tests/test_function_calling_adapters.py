#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import unittest

from openai.types.chat import ChatCompletionToolParam

from pipecat.services.function_calling.adapters.open_ai_adapter import OpenAIFunctionAdapter
from pipecat.services.function_calling.function_schema import FunctionSchema


class TestFunctionAdapters(unittest.TestCase):
    def setUp(self) -> None:
        """Sets up a common function schema for all tests."""
        self.function_def = FunctionSchema(
            name="get_weather",
            description="Get the weather in a given location",
            properties={
                "location": {"type": "string", "description": "The city, e.g. San Francisco"},
                "format": {
                    "type": "string",
                    "enum": ["celsius", "fahrenheit"],
                    "description": "The temperature unit to use.",
                },
            },
            required=["location", "format"],
        )

    def test_openai_adapter(self):
        """Test OpenAI adapter format transformation."""
        expected = ChatCompletionToolParam(
            type="function",
            function={
                "name": "get_weather",
                "description": "Get the weather in a given location",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {
                            "type": "string",
                            "description": "The city, e.g. San Francisco",
                        },
                        "format": {
                            "type": "string",
                            "enum": ["celsius", "fahrenheit"],
                            "description": "The temperature unit to use.",
                        },
                    },
                    "required": ["location", "format"],
                },
            },
        )
        assert OpenAIFunctionAdapter.to_provider_format(self.function_def) == expected