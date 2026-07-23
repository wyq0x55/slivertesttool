# coding: UTF-8
"""
Embedded, stable helper library baked into the unified framework.

These used to be separate project files (Bit.py, Extend.py, Wait.py,
CustomClass.py).  They are fixed logic/IO/timing/equality helpers, so they
live in code (not JSON).  They are auto-registered into the lib registry, so a
test/subroutine JSON can reference them by name:

    { "call": "Wait", "args": [50], "kind": "generator" }
    { "call": "Extend_CompareNvramWith", "args": ["nvram.txt"], "kind": "function",
      "store": "cmp" }

and MagicList can be used as a multi-value "expected" (observed == any-of-list).
"""

import os, sys, datetime

try:
    from synopsys.silver import *
except ImportError:
    try:
        from qtronic.silver import *
    except ImportError:
        Variable = None  # allows import under unit tests without Silver


# --------------------------------------------------------------------------- #
# CustomClass.py  ->  MagicList
# --------------------------------------------------------------------------- #
class MagicList(list):
    """A list that compares equal to any element it contains."""
    def __eq__(self, other):
        return other in self

    def __ne__(self, other):
        return other not in self

    __hash__ = None


# --------------------------------------------------------------------------- #
# Bit.py  ->  bit(pos, len, data)
# --------------------------------------------------------------------------- #
def bit(pos, len, data):
    return (data & (2 ** len - 1) << pos) >> pos


# --------------------------------------------------------------------------- #
# Wait.py  ->  Wait(ms) generator (yields True while waiting, False when done)
# --------------------------------------------------------------------------- #
def Wait(wait_time):
    time = Variable('currentTime')
    time_st = time.Value
    while True:
        if not (round(time.Value - time_st, 3) < float(wait_time) / 1000):
            break
        yield True
        continue
    yield False


# --------------------------------------------------------------------------- #
# Extend.py  ->  NVRAM comparison helpers
# --------------------------------------------------------------------------- #
_EXT_BASE = os.path.dirname(os.path.abspath(__file__))


def Extend_CompareNvramWith(argv):
    filename1 = os.path.normpath(os.path.join(os.path.dirname((sys.argv)[0]), argv))
    filename2 = os.path.normpath(os.path.join(_EXT_BASE, '../../../../SILS/TestIO/memory.final'))

    if "*" == argv:
        return "Default"
    if not os.path.exists(filename1):
        return "NotFile"
    if not os.path.exists(filename2):
        return "NotFile"

    alien1 = _ext_readfile(filename1)
    alien2 = _ext_readfile(filename2)

    for key, value in alien1.items():
        if key in alien2.keys():
            if not _ext_comparison(value, alien2[key]):
                return "Failed"
        else:
            return "Failed"
    return "Passed"


def _ext_readfile(filename):
    alienNvram = {}
    aKey = ''
    aValue = ''
    with open(filename, 'rb') as pf:
        for line in pf:
            if b':' in line:
                aKey = line.strip()
                aValue = b''
            else:
                aValue += line.strip()
            if aKey != '':
                alienNvram[aKey] = aValue
    return alienNvram


def _ext_comparison(str1, str2):
    li1 = list(str1)
    li2 = list(str2)
    for x in range(len(li1)):
        if li1[x] in (ord('*'), '*'):
            pass
        elif li1[x] != li2[x]:
            return 0
    return 1


# --------------------------------------------------------------------------- #
# Registry consumed by the runner
# --------------------------------------------------------------------------- #
BUILTINS = {
    'MagicList': MagicList,
    'bit': bit,
    'Wait': Wait,
    'Extend_CompareNvramWith': Extend_CompareNvramWith,
}
