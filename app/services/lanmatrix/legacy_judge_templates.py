"""Source templates for the legacy CSVLESS archive."""

PREAMBLE = """\
# coding: UTF-8
try:
    from synopsys.silver import *
    from synopsys.util import scheduler
except ImportError:
    from qtronic.silver import *
    from qtronic.util import scheduler
import datetime
import logging
import os
import sys
from decimal import Decimal

base = os.path.dirname(os.path.abspath(__file__))
"""

LIB_PATHS = """\
{case_path}root_path = os.path.normpath(os.path.join(base, {root_path!r}))
Lib_co_path = os.path.join(root_path, "02_Config/Library/Lib/")
Lib_std_path = os.path.join(root_path, "02_Config/Library/StdLib/")
sys.path.append(Lib_co_path)
sys.path.append(Lib_std_path)
from Common_Constant import *
from Constant import *
from Bit import *
from Extend import *
from Wait import *
"""

COMPLETION = """\

    if testResult == -1:
        print('Test is over. All steps is verified.')
        logging.info({passed})
    else:
        print('Test is failed in Step' + str(testResult) + '!!!!')
        logging.info('Test is failed in Step%s!!!!', testResult)
    print('Test is stoped at' + str(round(time.Value, digit)) + 's.')
    testPythonOver = 0
    for _ in range(10):
        yield
"""

USER_PLOT = """\
# coding: UTF-8
import logging
import os
import sys

base = os.path.dirname(os.path.abspath(__file__))
plot_path = os.path.normpath(os.path.join(base, '../../../../../04_Reference/03_Script/plot_script/'))
sys.path.append(plot_path)
from plot import plot_data

InList = {signals}
InMarkList = []
TestCaseName = {name}
InPutPath = './'
OutPutPath = '../../Graph/'

try:
    err_msg = plot_data(InPutPath, OutPutPath, TestCaseName, InList, InMarkList)
except Exception:
    logging.info(TestCaseName + ' is an error in the tool. Please contact the administrator.')
else:
    if err_msg:
        logging.info(TestCaseName + ': plot create failed because variable [' + err_msg + '] is not found in output.csv.')
    else:
        logging.info(TestCaseName + ': plot create passed .')
"""

STDLIB = {
    "Bit": """\
def bit(pos, length, data):
    return (data >> pos) & ((1 << length) - 1)
""",
    "CustomClass": """\
class MagicList(list):
    def __eq__(self, other):
        return other in self

    def __ne__(self, other):
        return other not in self
""",
    "Wait": """\
try:
    from synopsys.silver import Variable
except ImportError:
    from qtronic.silver import Variable


def Wait(wait_time):
    clock = Variable('currentTime')
    start = clock.Value
    while round(clock.Value - start, 3) < float(wait_time) / 1000:
        yield True
    yield False
""",
    "Extend": """\
import os
import sys

base = os.path.dirname(os.path.abspath(__file__))


def _read_nvram(filename):
    data = {}
    key = None
    with open(filename, 'rb') as stream:
        for line in stream:
            if b':' in line:
                key = line.strip()
                data[key] = b''
            elif key is not None:
                data[key] += line.strip()
    return data


def Extend_CompareNvramWith(argv):
    if argv == '*':
        return 'Default'
    expected_file = os.path.join(os.path.dirname(sys.argv[0]), argv)
    actual_file = os.path.join(base, '../../../../SILS/TestIO/memory.final')
    if not os.path.isfile(expected_file) or not os.path.isfile(actual_file):
        return 'NotFile'
    expected = _read_nvram(expected_file)
    actual = _read_nvram(actual_file)
    for key, value in expected.items():
        observed = actual.get(key, b'')
        if key not in actual or len(observed) < len(value):
            return 'Failed'
        if any(left != 42 and left != right for left, right in zip(value, observed)):
            return 'Failed'
    return 'Passed'
""",
}
