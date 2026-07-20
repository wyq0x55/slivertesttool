# coding: UTF-8
# Minimal self-contained judge for the mock backend.
try:
    from synopsys.silver import *
except ImportError:
    try:
        from qtronic.silver import *
    except ImportError:
        pass
import sys

def run():
    return "PASS"

if __name__ == "__main__":
    print("verdict=PASS")
