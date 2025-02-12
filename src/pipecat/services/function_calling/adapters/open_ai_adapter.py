#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

from openai.types.chat import ChatCompletionToolParam

from pipecat.services.function_calling.function_schema import FunctionSchema


class OpenAIFunctionAdapter:
    @staticmethod
    def to_provider_format(function_schema: FunctionSchema) -> ChatCompletionToolParam:
        """Converts the function schema to OpenAI's function-calling format.

        :return: OpenAI formatted function call definition.
        """
        return ChatCompletionToolParam(
            type="function",
            function=function_schema.to_dict()
        )