import logging

logger = logging.getLogger(__name__)

class ForexSignals:
    def __init__(self, data_manager):
        self._data = data_manager

    def get_signal(self, symbol):
        return None

    def get_all_signals(self):
        return []
