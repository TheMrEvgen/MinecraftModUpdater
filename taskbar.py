"""
Прогресс на значке программы в панели задач Windows (ITaskbarList3) без сторонних библиотек.
На других системах и при любой ошибке COM методы просто ничего не делают.
"""
import ctypes
import logging
import sys
from ctypes import wintypes

TBPF_NOPROGRESS, TBPF_INDETERMINATE, TBPF_NORMAL, TBPF_ERROR, TBPF_PAUSED = 0, 1, 2, 4, 8


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, text: str):
        super().__init__()
        ctypes.oledll.ole32.CLSIDFromString(text, ctypes.byref(self))


class TaskbarProgress:
    # Номера методов в таблице ITaskbarList3: IUnknown (0-2), ITaskbarList (3-7), ITaskbarList2 (8), ITaskbarList3 (9+)
    _HR_INIT, _SET_PROGRESS_VALUE, _SET_PROGRESS_STATE = 3, 9, 10

    def __init__(self, hwnd: int):
        self.hwnd = hwnd
        self._ptr = None
        self._state = None
        if sys.platform != "win32":
            return
        try:
            clsid = _GUID("{56FDF344-FD6D-11d0-958A-006097C9A090}")
            iid = _GUID("{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}")
            ptr = ctypes.c_void_p()
            ctypes.oledll.ole32.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(ptr))
            self._ptr = ptr
            self._call(self._HR_INIT, ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p))
        except OSError as e:
            logging.debug(f"Прогресс в панели задач недоступен: {e}")
            self._ptr = None

    def _call(self, index: int, prototype, *args):
        vtable = ctypes.cast(self._ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return prototype(vtable[index])(self._ptr, *args)

    def _set_state(self, state: int):
        if not self._ptr or state == self._state:
            return
        try:
            self._call(self._SET_PROGRESS_STATE,
                       ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, wintypes.HWND, ctypes.c_int),
                       self.hwnd, state)
            self._state = state
        except OSError as e:
            logging.debug(f"SetProgressState: {e}")

    def set_value(self, done: int, total: int, error: bool = False):
        if not self._ptr or total <= 0:
            return
        self._set_state(TBPF_ERROR if error else TBPF_NORMAL)
        try:
            self._call(self._SET_PROGRESS_VALUE,
                       ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, wintypes.HWND, ctypes.c_ulonglong,
                                          ctypes.c_ulonglong),
                       self.hwnd, max(0, min(done, total)), total)
        except OSError as e:
            logging.debug(f"SetProgressValue: {e}")

    def busy(self):
        self._set_state(TBPF_INDETERMINATE)

    def clear(self):
        self._set_state(TBPF_NOPROGRESS)
