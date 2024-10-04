# NEON AI (TM) SOFTWARE, Software Development Kit & Application Development System
# All trademark and other rights reserved by their respective owners
# Copyright 2008-2021 Neongecko.com Inc.
# BSD-3
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
# 3. Neither the name of the copyright holder nor the names of its
#    contributors may be used to endorse or promote products derived from this
#    software without specific prior written permission.
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO,
# THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR
# CONTRIBUTORS  BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA,
# OR PROFITS;  OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
# LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
# NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE,  EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import queue
from os import environ
from queue import Queue

from ovos_utils import wait_for_exit_signal

environ.setdefault("OVOS_CONFIG_BASE_FOLDER", "neon")
environ.setdefault("OVOS_CONFIG_FILENAME", "diana.yaml")

import io
import json
import requests

from os.path import join, isfile, dirname
from threading import Thread, Event
from base64 import b64decode, b64encode

from ovos_plugin_manager.microphone import OVOSMicrophoneFactory
from ovos_config.config import Configuration
from ovos_utils.messagebus import FakeBus
from ovos_utils.log import LOG
from ovos_bus_client.message import Message
from neon_utils.net_utils import get_adapter_info
from neon_utils.user_utils import get_default_user_config
from speech_recognition import AudioData
from pydub import AudioSegment
from pydub.playback import play
from websocket import WebSocketApp, ABNF

from neon_nodes import on_alive, on_error, on_ready, on_started, on_stopping, MockTransformers


class NeonAudioStreamClient:
    def __init__(self, bus=None, ready_hook=on_ready, error_hook=on_error,
                 stopping_hook=on_stopping, alive_hook=on_alive,
                 started_hook=on_started):
        self.error_hook = error_hook
        self.stopping_hook = stopping_hook
        alive_hook()
        self.config = Configuration()
        node_config = self.config["neon_node"]
        server_addr = node_config["hana_address"]
        self._connected = Event()
        self._watchdog_event = Event()

        auth_data = requests.post(f"{server_addr}/auth/login", json={
            "username": node_config["hana_username"],
            "password": node_config["hana_password"]}).json()
        LOG.info(auth_data)

        def ws_connect(*_, **__):
            self._connected.set()

        def ws_disconnect(*_, **__):
            if not self._connected.is_set():
                LOG.info("WS disconnected on shutdown")
                return
            error = "Websocket unexpectedly disconnected"
            self.error_hook(error)
            raise ConnectionError(error)

        def ws_error(_, exception):
            self.error_hook(exception)
            raise ConnectionError(f"Failed to connect: {exception}")

        ws_address = server_addr.replace("http", "ws", 1)
        self.websocket = WebSocketApp(f"{ws_address}/node/v1?token={auth_data['access_token']}",
                                      on_message=self._on_ws_data,
                                      on_open=ws_connect,
                                      on_error=ws_error,
                                      on_close=ws_disconnect)
        self.streaming_socket = WebSocketApp(
            f"{ws_address}/node/v1/stream?token={auth_data['access_token']}",
            on_message=self._on_ws_stream,
            on_open=ws_connect,
            on_error=ws_error,
            on_close=ws_disconnect)
        Thread(target=self.websocket.run_forever, daemon=True).start()
        self._wait_for_connection()
        self._connected.clear()
        Thread(target=self.streaming_socket.run_forever, daemon=True).start()
        self._device_data = self.config.get('neon_node', {})
        LOG.init(self.config.get("logging"))
        self.bus = bus or FakeBus()
        self.lang = self.config.get('lang') or "en-us"
        self._mic = OVOSMicrophoneFactory.create(self.config)
        self._mic.start()

        self.response_audio_queue = Queue()

        self._listening_sound = None
        self._error_sound = None

        self._network_info = dict()
        self._node_data = dict()
        self._stopping = False
        started_hook()
        self._voice_thread = Thread(target=self.run, daemon=True)
        self._wait_for_connection()
        self._voice_thread.start()
        ready_hook()
        wait_for_exit_signal()

    def _wait_for_connection(self):
        LOG.debug("Waiting for WS connection")
        if not self._connected.wait(30):
            error = f"Timeout waiting for connection to {self.websocket.url}"
            self.error_hook(error)
            raise TimeoutError(error)

    @property
    def listening_sound(self) -> AudioSegment:
        """
        Get an AudioSegment representation of the configured listening sound
        """
        if not self._listening_sound:
            res_file = Configuration().get('sounds').get('start_listening')
            if not isfile(res_file):
                res_file = join(dirname(__file__), "res", "start_listening.wav")
            self._listening_sound = AudioSegment.from_file(res_file,
                                                           format="wav")
        return self._listening_sound

    @property
    def error_sound(self) -> AudioSegment:
        """
        Get an AudioSegment representation of the configured error sound
        """
        if not self._error_sound:
            res_file = Configuration().get('sounds').get('error')
            if not isfile(res_file):
                res_file = join(dirname(__file__), "res", "error.wav")
            self._error_sound = AudioSegment.from_file(res_file, format="wav")
        return self._error_sound

    @property
    def network_info(self) -> dict:
        """
        Get networking information about this client, including IP addresses and
        MAC address.
        """
        if not self._network_info:
            self._network_info = get_adapter_info()
            public_ip = requests.get('https://api.ipify.org').text
            self._network_info["public"] = public_ip
            LOG.debug(f"Resolved network info: {self._network_info}")
        return self._network_info

    @property
    def node_data(self):
        """
        Get information about this node from configuration and networking status
        """
        if not self._node_data:
            self._node_data = {"device_description": self._node_data.get(
                'description', 'node voice client'),
                "networking": {
                    "local_ip": self.network_info.get('ipv4'),
                    "public_ip": self.network_info.get('public'),
                    "mac_address": self.network_info.get('mac')}
            }
            LOG.info(f"Resolved node_data: {self._node_data}")
        return self._node_data

    @property
    def user_profile(self) -> dict:
        """
        Get a user profile from local disk
        """
        return get_default_user_config()

    def _on_ws_stream(self, _, wav_bytes, *__):
        LOG.info(f"len(wav_bytes): {len(wav_bytes)}")
        self.response_audio_queue.put(wav_bytes)

    def _on_ws_data(self, _, serialized: str):
        try:
            message = Message.deserialize(serialized)
            self.on_response(message)
        except Exception as e:
            LOG.exception(e)

    def run(self):
        """
        Start the voice thread as a daemon and return
        """
        self._stopping = False
        while not self._stopping:
            try:
                if audio_bytes := self.response_audio_queue.get(block=False):
                    LOG.info(f"Play response audio")
                    play(AudioSegment.from_file(io.BytesIO(audio_bytes),
                                                format="wav"))
            except queue.Empty:
                pass
            byte_audio = self._mic.read_chunk()
            self.streaming_socket.send(byte_audio, ABNF.OPCODE_BINARY)

    def watchdog(self):
        """
        Runs in a loop to make sure the voice loop is running. If the loop is
        unexpectedly stopped, raise an exception to kill this process.
        """
        try:
            while not self._watchdog_event.wait(30):
                pass
                # TODO: Call `error_hook` with an error code if something is wrong
        except KeyboardInterrupt:
            self.shutdown()

    def on_stt_audio(self, audio_bytes: bytes, context: dict):
        """
        Callback when there is a recorded STT segment.
        @param audio_bytes: bytes of recorded audio
        @param context: dict context associated with recorded audio
        """
        LOG.debug(f"Got {len(audio_bytes)} bytes of audio")
        wav_data = AudioData(audio_bytes, self._mic.sample_rate,
                             self._mic.sample_width).get_wav_data()
        try:
            self.on_input(wav_data)
        except Exception as e:
            play(self.error_sound)
            # Unknown error, restart to be safe
            self.error_hook(repr(e))
            raise e

    def on_hotword_audio(self, audio: bytes, context: dict):
        """
        Callback when a hotword is detected.
        @param audio: bytes of detected hotword audio
        @param context: dict context associated with recorded hotword
        """
        payload = context
        msg_type = "recognizer_loop:wakeword"
        play(self.listening_sound)
        LOG.info(f"Emitting hotword event: {msg_type}")
        # emit ww event
        self.bus.emit(Message(msg_type, payload, context))
        # TODO: Optionally save/upload hotword audio

    def on_input(self, audio: bytes):
        """
        Handle recorded audio input and get/speak a response.
        @param audio: bytes of STT audio
        """
        audio_data = b64encode(audio).decode("utf-8")
        data = {"msg_type": "neon.audio_input",
                "data": {"audio_data": audio_data, "lang": self.lang}}
        self.websocket.send(json.dumps(data))

    def on_response(self, message: Message):
        if message.msg_type == "klat.response":
            # Handled as audio stream
            pass
            # LOG.info(f"Response="
            #          f"{message.data['responses'][self.lang]['sentence']}")
            # encoded_audio = message.data['responses'][self.lang]['audio']
            # audio_bytes = b64decode(encoded_audio.get('female') or
            #                         encoded_audio.get('male'))
            # play(AudioSegment.from_file(io.BytesIO(audio_bytes), format="wav"))
            # LOG.info(f"Playback completed")
        elif message.msg_type == "neon.ww_detected":
            play(self.listening_sound)
        elif message.msg_type == "neon.alert_expired":
            LOG.info(f"Alert expired: {message.data}")
        elif message.msg_type == "neon.audio_input.response":
            LOG.info(f"Got STT: {message.data.get('transcripts')}")
        else:
            LOG.warning(f"Ignoring message: {message.msg_type}")

    def shutdown(self):
        """
        Cleanly stop all threads and shutdown this service
        """
        self._stopping = True
        self.stopping_hook()
        self._connected.clear()
        self.response_audio_queue.put(None)
        self.websocket.close()
        self._voice_thread.join(30)


def main(*args, **kwargs):
    client = NeonAudioStreamClient(*args, **kwargs)
    client.watchdog()


if __name__ == "__main__":
    main()
