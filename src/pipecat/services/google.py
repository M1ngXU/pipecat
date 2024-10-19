#
# Copyright (c) 2024, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import asyncio
from dataclasses import dataclass
import json
import io
from typing import AsyncGenerator, List, Literal, Optional


from loguru import logger
from PIL import Image
from pydantic import BaseModel

from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMMessagesFrame,
    LLMUpdateSettingsFrame,
    TextFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    VisionImageRawFrame,
)
from pipecat.processors.aggregators.openai_llm_context import (
    OpenAILLMContext,
    OpenAILLMContextFrame,
)
from pipecat.services.openai import (
    OpenAIAssistantContextAggregator,
    OpenAIUserContextAggregator,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.ai_services import LLMService, TTSService
from pipecat.transcriptions.language import Language

try:
    import google.ai.generativelanguage as glm
    import google.generativeai as gai
    from google.cloud import texttospeech_v1
    from google.oauth2 import service_account
except ModuleNotFoundError as e:
    logger.error(f"Exception: {e}")
    logger.error(
        "In order to use Google AI, you need to `pip install pipecat-ai[google]`. Also, set the environment variable GOOGLE_API_KEY for the GoogleLLMService and GOOGLE_APPLICATION_CREDENTIALS for the GoogleTTSService`."
    )
    raise Exception(f"Missing module: {e}")


class GoogleUserContextAggregator(OpenAIUserContextAggregator):
    async def _push_aggregation(self):
        if len(self._aggregation) > 0:
            self._context.add_message({"role": "user", "parts": [glm.Part(text=self._aggregation)]})

            # Reset the aggregation. Reset it before pushing it down, otherwise
            # if the tasks gets cancelled we won't be able to clear things up.
            self._aggregation = ""

            frame = OpenAILLMContextFrame(self._context)
            await self.push_frame(frame)

            # Reset our accumulator state.
            self._reset()


class GoogleAssistantContextAggregator(OpenAIAssistantContextAggregator):
    async def _push_aggregation(self):
        if not (
            self._aggregation or self._function_call_result or self._pending_image_frame_message
        ):
            return

        run_llm = False

        aggregation = self._aggregation
        self._reset()

        try:
            if self._function_call_result:
                frame = self._function_call_result
                self._function_call_result = None
                if frame.result:
                    logger.debug(f"FunctionCallResultFrame result: {frame.arguments}")
                    self._context.add_message(
                        {
                            "role": "model",
                            "parts": [
                                glm.Part(
                                    function_call=glm.FunctionCall(
                                        name=frame.function_name, args=frame.arguments
                                    )
                                )
                            ],
                        }
                    )
                    response = frame.result
                    if isinstance(response, str):
                        response = {"response": response}
                    self._context.add_message(
                        {
                            "role": "user",
                            "parts": [
                                glm.Part(
                                    function_response=glm.FunctionResponse(
                                        name=frame.function_name, response=response
                                    )
                                )
                            ],
                        }
                    )
                    run_llm = not bool(self._function_calls_in_progress)
            else:
                self._context.add_message({"role": "model", "parts": [glm.Part(text=aggregation)]})

            if self._pending_image_frame_message:
                frame = self._pending_image_frame_message
                self._pending_image_frame_message = None
                self._context.add_image_frame_message(
                    format=frame.user_image_raw_frame.format,
                    size=frame.user_image_raw_frame.size,
                    image=frame.user_image_raw_frame.image,
                    text=frame.text,
                )
                run_llm = True

            if run_llm:
                await self._user_context_aggregator.push_context_frame()

            frame = OpenAILLMContextFrame(self._context)
            await self.push_frame(frame)

        except Exception as e:
            logger.exception(f"Error processing frame: {e}")


@dataclass
class GoogleContextAggregatorPair:
    _user: "GoogleUserContextAggregator"
    _assistant: "GoogleAssistantContextAggregator"

    def user(self) -> "GoogleUserContextAggregator":
        return self._user

    def assistant(self) -> "GoogleAssistantContextAggregator":
        return self._assistant


class GoogleLLMContext(OpenAILLMContext):
    @staticmethod
    def upgrade_to_google(obj: OpenAILLMContext) -> "GoogleLLMContext":
        if isinstance(obj, OpenAILLMContext) and not isinstance(obj, GoogleLLMContext):
            logger.debug(f"Upgrading to Google: {obj}")
            obj.__class__ = GoogleLLMContext
            obj._restructure_from_openai_messages()
        return obj

    def from_standard_message(self, message):
        role = message["role"]
        content = message["content"]
        if role == "system":
            role = "user"
        elif role == "assistant":
            role = "model"

        parts = []
        if isinstance(content, str):
            parts.append(glm.Part(text=content))
        elif isinstance(content, list):
            logger.debug("!!!NEED TO IMPL CONTENT LIST")

        message = {"role": role, "parts": parts}
        return message

    def add_image_frame_message(
        self, *, format: str, size: tuple[int, int], image: bytes, text: str = None
    ):
        buffer = io.BytesIO()
        Image.frombytes(format, size, image).save(buffer, format="JPEG")

        parts = []
        if text:
            parts.append(glm.Part(text=text))
        parts.append(
            glm.Part(inline_data=glm.Blob(mime_type="image/jpeg", data=buffer.getvalue())),
        )
        self.add_message({"role": "user", "parts": parts})

    def _restructure_from_openai_messages(self):
        self._messages[:] = [self.from_standard_message(m) for m in self._messages]


class GoogleLLMService(LLMService):
    """This class implements inference with Google's AI models

    This service translates internally from OpenAILLMContext to the messages format
    expected by the Google AI model. We are using the OpenAILLMContext as a lingua
    franca for all LLM services, so that it is easy to switch between different LLMs.
    """

    def __init__(self, *, api_key: str, model: str = "gemini-1.5-flash-latest", **kwargs):
        super().__init__(**kwargs)
        gai.configure(api_key=api_key)
        self._create_client(model)

    def can_generate_metrics(self) -> bool:
        return True

    def _create_client(self, model: str):
        self.set_model_name(model)
        self._client = gai.GenerativeModel(model)

    def _get_messages_from_openai_context(self, context: OpenAILLMContext) -> List[glm.Content]:
        openai_messages = context.get_messages()
        google_messages = []

        for message in openai_messages:
            role = message["role"]
            content = message["content"]
            if role == "system":
                role = "user"
            elif role == "assistant":
                role = "model"

            parts = [glm.Part(text=content)]
            if "mime_type" in message:
                parts.append(
                    glm.Part(
                        inline_data=glm.Blob(
                            mime_type=message["mime_type"], data=message["data"].getvalue()
                        )
                    )
                )
            google_messages.append({"role": role, "parts": parts})

        return google_messages

    async def _async_generator_wrapper(self, sync_generator):
        for item in sync_generator:
            yield item
            await asyncio.sleep(0)

    async def _process_context(self, context: OpenAILLMContext):
        await self.push_frame(LLMFullResponseStartFrame())
        try:
            logger.debug(f"Generating chat: {context.messages}")

            # todo: move this into the new context code structure, convert from openai context one time
            # todo: add system instructions
            # messages = self._get_messages_from_openai_context(context)
            messages = context.messages

            await self.start_ttfb_metrics()

            tools = context.tools if context.tools else []
            response = self._client.generate_content(contents=messages, tools=tools, stream=True)

            await self.stop_ttfb_metrics()

            async for chunk in self._async_generator_wrapper(response):
                # todo: usage
                try:
                    for c in chunk.parts:
                        if c.text:
                            await self.push_frame(TextFrame(c.text))
                        elif c.function_call:
                            args = type(c.function_call).to_dict(c.function_call).get("args", {})
                            await self.call_function(
                                context=context,
                                tool_call_id="what_should_this_be",
                                function_name=c.function_call.name,
                                arguments=args,
                            )
                except Exception as e:
                    # Google LLMs seem to flag safety issues a lot!
                    if chunk.candidates[0].finish_reason == 3:
                        logger.debug(
                            f"LLM refused to generate content for safety reasons - {messages}."
                        )
                    else:
                        logger.exception(f"{self} error: {e}")

        except Exception as e:
            logger.exception(f"{self} exception: {e}")
        finally:
            await self.push_frame(LLMFullResponseEndFrame())

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        context = None

        if isinstance(frame, OpenAILLMContextFrame):
            context: GoogleLLMContext = GoogleLLMContext.upgrade_to_google(frame.context)
        elif isinstance(frame, LLMMessagesFrame):
            context = GoogleLLMContext(frame.messages)
        elif isinstance(frame, VisionImageRawFrame):
            # todo: fix this
            context = OpenAILLMContext.from_image_frame(frame)
        elif isinstance(frame, LLMUpdateSettingsFrame):
            await self._update_settings(frame.settings)
        else:
            await self.push_frame(frame, direction)

        if context:
            await self._process_context(context)

    @staticmethod
    def create_context_aggregator(
        context: OpenAILLMContext, *, assistant_expect_stripped_words: bool = True
    ) -> GoogleContextAggregatorPair:
        user = GoogleUserContextAggregator(context)
        assistant = GoogleAssistantContextAggregator(
            user, expect_stripped_words=assistant_expect_stripped_words
        )
        return GoogleContextAggregatorPair(_user=user, _assistant=assistant)


class GoogleTTSService(TTSService):
    class InputParams(BaseModel):
        pitch: Optional[str] = None
        rate: Optional[str] = None
        volume: Optional[str] = None
        emphasis: Optional[Literal["strong", "moderate", "reduced", "none"]] = None
        language: Optional[Language] = Language.EN
        gender: Optional[Literal["male", "female", "neutral"]] = None
        google_style: Optional[Literal["apologetic", "calm", "empathetic", "firm", "lively"]] = None

    def __init__(
        self,
        *,
        credentials: Optional[str] = None,
        credentials_path: Optional[str] = None,
        voice_id: str = "en-US-Neural2-A",
        sample_rate: int = 24000,
        params: InputParams = InputParams(),
        **kwargs,
    ):
        super().__init__(sample_rate=sample_rate, **kwargs)

        self._settings = {
            "sample_rate": sample_rate,
            "pitch": params.pitch,
            "rate": params.rate,
            "volume": params.volume,
            "emphasis": params.emphasis,
            "language": self.language_to_service_language(params.language)
            if params.language
            else Language.EN,
            "gender": params.gender,
            "google_style": params.google_style,
        }
        self.set_voice(voice_id)
        self._client: texttospeech_v1.TextToSpeechAsyncClient = self._create_client(
            credentials, credentials_path
        )

    def _create_client(
        self, credentials: Optional[str], credentials_path: Optional[str]
    ) -> texttospeech_v1.TextToSpeechAsyncClient:
        creds: Optional[service_account.Credentials] = None

        # Create a Google Cloud service account for the Cloud Text-to-Speech API
        # Using either the provided credentials JSON string or the path to a service account JSON
        # file, create a Google Cloud service account and use it to authenticate with the API.
        if credentials:
            # Use provided credentials JSON string
            json_account_info = json.loads(credentials)
            creds = service_account.Credentials.from_service_account_info(json_account_info)
        elif credentials_path:
            # Use service account JSON file if provided
            creds = service_account.Credentials.from_service_account_file(credentials_path)

        return texttospeech_v1.TextToSpeechAsyncClient(credentials=creds)

    def can_generate_metrics(self) -> bool:
        return True

    def language_to_service_language(self, language: Language) -> str | None:
        match language:
            case Language.BG:
                return "bg-BG"
            case Language.CA:
                return "ca-ES"
            case Language.ZH:
                return "cmn-CN"
            case Language.ZH_TW:
                return "cmn-TW"
            case Language.CS:
                return "cs-CZ"
            case Language.DA:
                return "da-DK"
            case Language.NL:
                return "nl-NL"
            case Language.EN | Language.EN_US:
                return "en-US"
            case Language.EN_AU:
                return "en-AU"
            case Language.EN_GB:
                return "en-GB"
            case Language.EN_IN:
                return "en-IN"
            case Language.ET:
                return "et-EE"
            case Language.FI:
                return "fi-FI"
            case Language.NL_BE:
                return "nl-BE"
            case Language.FR:
                return "fr-FR"
            case Language.FR_CA:
                return "fr-CA"
            case Language.DE:
                return "de-DE"
            case Language.EL:
                return "el-GR"
            case Language.HI:
                return "hi-IN"
            case Language.HU:
                return "hu-HU"
            case Language.ID:
                return "id-ID"
            case Language.IT:
                return "it-IT"
            case Language.JA:
                return "ja-JP"
            case Language.KO:
                return "ko-KR"
            case Language.LV:
                return "lv-LV"
            case Language.LT:
                return "lt-LT"
            case Language.MS:
                return "ms-MY"
            case Language.NO:
                return "nb-NO"
            case Language.PL:
                return "pl-PL"
            case Language.PT:
                return "pt-PT"
            case Language.PT_BR:
                return "pt-BR"
            case Language.RO:
                return "ro-RO"
            case Language.RU:
                return "ru-RU"
            case Language.SK:
                return "sk-SK"
            case Language.ES:
                return "es-ES"
            case Language.SV:
                return "sv-SE"
            case Language.TH:
                return "th-TH"
            case Language.TR:
                return "tr-TR"
            case Language.UK:
                return "uk-UA"
            case Language.VI:
                return "vi-VN"
        return None

    def _construct_ssml(self, text: str) -> str:
        ssml = "<speak>"

        # Voice tag
        voice_attrs = [f"name='{self._voice_id}'"]

        language = self._settings["language"]
        voice_attrs.append(f"language='{language}'")

        if self._settings["gender"]:
            voice_attrs.append(f"gender='{self._settings['gender']}'")
        ssml += f"<voice {' '.join(voice_attrs)}>"

        # Prosody tag
        prosody_attrs = []
        if self._settings["pitch"]:
            prosody_attrs.append(f"pitch='{self._settings['pitch']}'")
        if self._settings["rate"]:
            prosody_attrs.append(f"rate='{self._settings['rate']}'")
        if self._settings["volume"]:
            prosody_attrs.append(f"volume='{self._settings['volume']}'")

        if prosody_attrs:
            ssml += f"<prosody {' '.join(prosody_attrs)}>"

        # Emphasis tag
        if self._settings["emphasis"]:
            ssml += f"<emphasis level='{self._settings['emphasis']}'>"

        # Google style tag
        if self._settings["google_style"]:
            ssml += f"<google:style name='{self._settings['google_style']}'>"

        ssml += text

        # Close tags
        if self._settings["google_style"]:
            ssml += "</google:style>"
        if self._settings["emphasis"]:
            ssml += "</emphasis>"
        if prosody_attrs:
            ssml += "</prosody>"
        ssml += "</voice></speak>"

        return ssml

    async def run_tts(self, text: str) -> AsyncGenerator[Frame, None]:
        logger.debug(f"Generating TTS: [{text}]")

        try:
            await self.start_ttfb_metrics()

            ssml = self._construct_ssml(text)
            synthesis_input = texttospeech_v1.SynthesisInput(ssml=ssml)
            voice = texttospeech_v1.VoiceSelectionParams(
                language_code=self._settings["language"], name=self._voice_id
            )
            audio_config = texttospeech_v1.AudioConfig(
                audio_encoding=texttospeech_v1.AudioEncoding.LINEAR16,
                sample_rate_hertz=self._settings["sample_rate"],
            )

            request = texttospeech_v1.SynthesizeSpeechRequest(
                input=synthesis_input, voice=voice, audio_config=audio_config
            )

            response = await self._client.synthesize_speech(request=request)

            await self.start_tts_usage_metrics(text)

            yield TTSStartedFrame()

            # Skip the first 44 bytes to remove the WAV header
            audio_content = response.audio_content[44:]

            # Read and yield audio data in chunks
            chunk_size = 8192
            for i in range(0, len(audio_content), chunk_size):
                chunk = audio_content[i : i + chunk_size]
                if not chunk:
                    break
                await self.stop_ttfb_metrics()
                frame = TTSAudioRawFrame(chunk, self._settings["sample_rate"], 1)
                yield frame
                await asyncio.sleep(0)  # Allow other tasks to run

            yield TTSStoppedFrame()

        except Exception as e:
            logger.exception(f"{self} error generating TTS: {e}")
            error_message = f"TTS generation error: {str(e)}"
            yield ErrorFrame(error=error_message)
        finally:
            yield TTSStoppedFrame()
