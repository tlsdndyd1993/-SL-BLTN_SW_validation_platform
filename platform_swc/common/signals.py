# -*- coding: utf-8 -*-
"""common/signals.py — 프로그램 전역 PyQt5 시그널 허브."""
from PyQt5.QtCore import QObject, pyqtSignal


class Signals(QObject):
    blackout_detected       = pyqtSignal(str, dict)
    status_message          = pyqtSignal(str)
    ac_count_changed        = pyqtSignal(int)
    ac_state_changed        = pyqtSignal(bool)
    manual_settings_changed = pyqtSignal(float, float)  # pre_sec, post_sec
    manual_source_changed   = pyqtSignal(str)
    rec_started             = pyqtSignal(str)
    rec_stopped             = pyqtSignal()
    macro_step_rec          = pyqtSignal(object)
    manual_clip_saved       = pyqtSignal(str)
    cameras_scanned         = pyqtSignal(list)
    monitors_scanned        = pyqtSignal(list)
    capture_saved           = pyqtSignal(str, str)
    tc_verify_request       = pyqtSignal()
    roi_list_changed        = pyqtSignal()
    blackout_rec_changed    = pyqtSignal(bool)
    blackout_log            = pyqtSignal(str)
    kernel_log              = pyqtSignal(str)
