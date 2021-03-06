# Copyright 2017 Mycroft AI Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import tempfile
import time

import os
from os.path import dirname, exists, join, abspath

from mycroft.configuration import Configuration
from mycroft.util.log import LOG
import traceback
import sys


RECOGNIZER_DIR = join(abspath(dirname(__file__)), "recognizer")


class HotWordEngine(object):
    def __init__(self, key_phrase="hey mycroft", config=None, lang="en-us"):
        self.lang = str(lang).lower()
        self.key_phrase = str(key_phrase).lower()
        # rough estimate 1 phoneme per 2 chars
        self.num_phonemes = len(key_phrase) / 2 + 1
        if config is None:
            config = Configuration.get().get("hot_words", {})
            config = config.get(self.key_phrase, {})
        self.config = config
        self.listener_config = Configuration.get().get("listener", {})

    def found_wake_word(self, frame_data):
        return False


class PocketsphinxHotWord(HotWordEngine):
    def __init__(self, key_phrase="hey mycroft", config=None, lang="en-us"):
        super(PocketsphinxHotWord, self).__init__(key_phrase, config, lang)
        # Hotword module imports
        from pocketsphinx import Decoder
        # Hotword module config
        module = self.config.get("module","pocketsphinx")
        if module != "pocketsphinx":
            LOG.warning(
                str(module) + " module does not match with "
                              "Hotword class pocketsphinx")
        # Hotword module params
        self.phonemes = self.config.get("phonemes", "HH EY . M AY K R AO F T")
	print("self.phonemes-->", self.phonemes)
        self.num_phonemes = len(self.phonemes.split())
	print("self.phonemes-->", self.num_phonemes)
        self.threshold = self.config.get("threshold", 1e-90)
        self.sample_rate = self.listener_config.get("sample_rate", 1600)
        dict_name = self.create_dict(self.key_phrase, self.phonemes)
	print("dict_name-->",dict_name)
        config = self.create_config(dict_name, Decoder.default_config())
        self.decoder = Decoder(config)

    def create_dict(self, key_phrase, phonemes):
        (fd, file_name) = tempfile.mkstemp()
        words = key_phrase.split()
        phoneme_groups = phonemes.split('.')
        with os.fdopen(fd, 'w') as f:
            for word, phoneme in zip(words, phoneme_groups):
                f.write(word + ' ' + phoneme + '\n')
        return file_name

    def create_config(self, dict_name, config):
        model_file = join(RECOGNIZER_DIR, 'model', self.lang, 'hmm')
	print("&&&& model_file-->",model_file)
        if not exists(model_file):
            LOG.error('PocketSphinx model not found at ' + str(model_file))
        config.set_string('-hmm', model_file)
        config.set_string('-dict', dict_name)
        config.set_string('-keyphrase', self.key_phrase)
        config.set_float('-kws_threshold', float(self.threshold))
        config.set_float('-samprate', self.sample_rate)
        config.set_int('-nfft', 2048)
        config.set_string('-logfn', '/home/sg/mycroft-core/scripts/logs/pocket.log')
        return config

    def transcribe(self, byte_data, metrics=None):
        start = time.time()
        self.decoder.start_utt()
        self.decoder.process_raw(byte_data, False, False)
        self.decoder.end_utt()
        if metrics:
            metrics.timer("mycroft.stt.local.time_s", time.time() - start)
    #	LOG.info("transcribed into--->",self.decoder.hyp().hypstr.lower())
        return self.decoder.hyp()

    def found_wake_word(self, frame_data):
        hyp = self.transcribe(frame_data)
        return hyp and self.key_phrase in hyp.hypstr.lower()


class SnowboyHotWord(HotWordEngine):
    def __init__(self, key_phrase="hey mycroft", config=None, lang="en-us"):
        super(SnowboyHotWord, self).__init__(key_phrase, config, lang)
        # Hotword module imports
        from snowboydecoder import HotwordDetector
        # Hotword module config
        module = self.config.get("module")
        if module != "snowboy":
            LOG.warning(module + " module does not match with Hotword class "
                                 "snowboy")
        # Hotword params
        models = self.config.get("models", {})
        paths = []
        for key in models:
            paths.append(models[key])
        sensitivity = self.config.get("sensitivity", 0.5)
        self.snowboy = HotwordDetector(paths,
                                       sensitivity=[sensitivity] * len(paths))
        self.lang = str(lang).lower()
        self.key_phrase = str(key_phrase).lower()

    def found_wake_word(self, frame_data):
        wake_word = self.snowboy.detector.RunDetection(frame_data)
        return wake_word == 1


class HotWordFactory(object):
    CLASSES = {
        "pocketsphinx": PocketsphinxHotWord,
        "snowboy": SnowboyHotWord
    }

    @staticmethod
    def create_hotword(hotword="hey mycroft", config=None, lang="en-us"):
        LOG.info("creating " + hotword)
        if not config:
            config = Configuration.get().get("hotwords", {})
        module = config.get(hotword).get("module", "pocketsphinx")
        config = config.get(hotword, {"module": "pocketsphinx"})
        clazz = HotWordFactory.CLASSES.get(module)
        try:
            return clazz(hotword, config, lang=lang)
        except (KeyboardInterrupt, SystemExit):
            raise
        except:
            LOG.exception('Could not create hotword. Falling back to default.')
	    print(traceback.format_exc())
            return HotWordFactory.CLASSES['pocketsphinx']()
