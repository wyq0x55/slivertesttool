"""Export LAN Matrix procedures as CSVLESS Silver scripts."""
from __future__ import annotations

import ast
import builtins
import io
import keyword
import math
import re
import tokenize
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import PureWindowsPath

from ...runners.run_layout import sanitize_segment
from . import silver_json_export as matrix
from .errors import ServiceError
from .legacy_judge_templates import (
    COMPLETION, LIB_PATHS, PREAMBLE, STDLIB, USER_PLOT,
)


class LegacyExportError(ServiceError):
    def __init__(self, message):
        super().__init__(message, code="LEGACY_JUDGE_EXPORT")


_BLANK = {"", "-", "*", "―", "ー", "─", "‐"}
_BUILTINS = {"Wait", "Extend_CompareNvramWith"}
_BIT = re.compile(r"bit\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(.+?)\s*\)")
_REFERENCE = re.compile(r"\$([^$\r\n]+)\$")
_OPERATOR = re.compile(r"^(==|!=|>=|<=|>|<|=)")
_OPERATORS = str.maketrans({"≠": "!=", "≧": ">=", "≦": "<=", "≥": ">=",
                            "≤": "<=", "→": "_>", "、": ","})
_EXPRESSION_NODES = (
    ast.Expression, ast.Constant, ast.Name, ast.Load, ast.Tuple, ast.List,
    ast.BinOp, ast.UnaryOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv,
    ast.Mod, ast.Pow, ast.BitAnd, ast.BitOr, ast.BitXor, ast.LShift, ast.RShift,
    ast.UAdd, ast.USub, ast.Invert,
)
_RESERVED = set(dir(builtins)) | {
    "Bit", "Constant", "Common_Constant", "CustomClass", "Extend", "MagicList",
    "DLL_OK", "Variable", "MainGenerator", "pre_init", "pre_cleanup", "judge",
    "user", "base", "digit", "time", "stepsize", "logging", "os", "sys",
    "Decimal", "datetime", "scheduler", "bit", "cmt_g", "logfile", "testPythonOver",
    "testStepNo", "time_st", "isSuccessful", "testResult", "gen", "extended",
    "Lib_in_path", "Lib_co_path", "Lib_std_path", "root_path",
}


def _text(value):
    return "" if value is None else str(value).strip()


def _normalize(value):
    return unicodedata.normalize("NFKC", _text(value)).translate(_OPERATORS)


def _identifier(name):
    if (not name.isidentifier() or keyword.iskeyword(name)
            or unicodedata.normalize("NFKC", name) != name
            or name in _RESERVED or re.fullmatch(r"dat\d+", name)):
        raise LegacyExportError(f"Invalid Python identifier: {name!r}")


def _segment(name):
    if sanitize_segment(name, "") != name or not name or PureWindowsPath(name).is_reserved():
        raise LegacyExportError(f"Invalid export directory name: {name!r}")
    return name


class Signals:
    def __init__(self, body):
        self.inputs = matrix._signal_pairs(body.get("input_signals"))
        self.expected = matrix._signal_pairs(body.get("expected_signals"))
        self.variables = {}
        self.labels = {}
        for label, path in self.inputs + self.expected:
            if path.strip() and "T.B.D" not in path:
                self.bind(path, label)

    def bind(self, path, label=""):
        path = path.strip()
        bit = _BIT.fullmatch(path)
        if bit:
            _, _, path = bit.groups()
        if not path or "\n" in path or "\r" in path or path.startswith("bit("):
            raise LegacyExportError(f"Invalid signal path: {path!r}")
        variable = self.variables.setdefault(path, f"dat{len(self.variables)}")
        self.labels.setdefault(path, label or path)
        if bit:
            return f"bit({int(bit[1])}, {int(bit[2])}, int({variable}.Value))"
        return f"{variable}.Value"


def _expression(text, names, signals=None):
    references = {}

    def replace(match):
        if signals is None:
            raise LegacyExportError(f"Signal reference outside a procedure: {match[0]}")
        value = signals.bind(match[1])
        if re.search(r"[&|^~]|<<|>>", text):
            value = f"int({value})"
        alias = f"__silver_signal_{len(references)}"
        while alias in text:
            alias += "_"
        references[alias] = value
        return alias

    src = _REFERENCE.sub(replace, text)
    try:
        tree = ast.parse(src.strip(), mode="eval")
    except SyntaxError as exc:
        raise LegacyExportError(f"Invalid expression: {text!r}") from exc
    for node in ast.walk(tree):
        if not isinstance(node, _EXPRESSION_NODES):
            raise LegacyExportError(f"Unsupported expression: {text!r}")
        if isinstance(node, ast.Constant) and (
                not isinstance(node.value, (str, int, float, bool))
                or isinstance(node.value, float) and not math.isfinite(node.value)):
            raise LegacyExportError(f"Unsupported literal: {text!r}")
        if isinstance(node, ast.Name) and node.id not in names and node.id not in references:
            raise LegacyExportError(f"Undefined constant or parameter: {node.id}")
    result = ast.unparse(tree)
    if references:
        result = re.sub(r"\b__silver_signal_\w+\b",
                        lambda match: references.get(match[0], match[0]), result)
    return result


def _cells(step, key, columns):
    values = step.get(key, [])
    if not isinstance(values, list) or len(values) > len(columns):
        raise LegacyExportError(f"{key} do not match the signal columns")
    for (label, path), value in zip(columns, values):
        raw = _text(value)
        normalized = _normalize(raw)
        label_value, expr = (("", normalized) if normalized.startswith("MagicList(")
                             else matrix._split_cell(normalized))
        if expr not in _BLANK:
            yield label, path.strip(), label_value or raw, expr


def _timing(raw, default):
    value = _normalize(raw)
    mode = re.search(r"\b(WAIT|WATCH|JUDGE)\b", value, re.I)
    info = matrix._parse_timing(value)
    method = mode[0].upper() if mode else info.method
    if re.search(r"[-−]\s*(?:\d|\.\d)", value):
        raise LegacyExportError(f"Invalid timing: {raw!r}")
    if method == matrix.JUDGE_IMMEDIATE:
        return method, 0.001, "即座"
    if method in {matrix.JUDGE_UNTIL, matrix.JUDGE_KEEP}:
        if info.seconds is None or not math.isfinite(info.seconds):
            raise LegacyExportError(f"Invalid timing: {raw!r}")
        return method, info.seconds, info.display
    if value not in _BLANK | {"任意", "成立するまで"}:
        raise LegacyExportError(f"Unknown timing: {raw!r}")
    return method, default, "任意"


@dataclass
class Check:
    target: str
    expected: str
    observed: str | None = None
    condition: str | None = None

    def logs(self):
        lines = []
        if self.condition:
            lines.append(f"logging.info('●' if {self.condition} else '▲')")
        else:
            lines.append("logging.info('▼')")
        lines.extend([
            f"logging.info({('    Monitoring target: ' + self.target)!r})",
            f"logging.info({('    Expected Value:' + self.expected)!r})",
            (f"logging.info('    Observed Value:%s ( %s ) ', "
             f"hex(int({self.observed})), {self.observed})" if self.observed
             else "logging.info('    Observed Value:-(-)')"),
        ])
        return lines


def _check(label, path, title, expr, names, signals):
    check = Check(f"{label} ( {path} ) ", title)
    if "T.B.D" in path or "T.B.D" in expr:
        return check
    observed = signals.bind(path)

    def value(src):
        return _expression(src, names, signals)

    def compare(part):
        part = part.strip()
        op = _OPERATOR.match(part)
        symbol = op[0] if op and op[0] != "=" else "=="
        return f"({observed} {symbol} {value(part[op.end():] if op else part)})"

    if "_>" in expr:
        before, _, after = expr.partition("_>")
        if _BIT.fullmatch(path):
            raise LegacyExportError("Transition checks require an unmasked signal")
        variable = observed.removesuffix(".Value")
        initial = (f"{variable}.duration == 0" if before.strip() == "*"
                   else f"{variable}.prevValue == {value(before)}")
        final = (f"{variable}.duration == 0" if after.strip() == "*"
                 else f"{observed} == {value(after)}")
        condition = f"({initial} and {final})"
    elif "||" in expr:
        condition = "(" + " or ".join(compare(part) for part in expr.split("||")) + ")"
    elif expr[0] in "[(" and expr[-1] in "])" and matrix._INTERVAL_CELL_RE.fullmatch(expr):
        lower, upper = (bound.strip() for bound in expr[1:-1].split(",", 1))
        bounds = []
        if lower:
            bounds.append(f"{observed} {'>=' if expr[0] == '[' else '>'} {value(lower)}")
        if upper:
            bounds.append(f"{observed} {'<=' if expr[-1] == ']' else '<'} {value(upper)}")
        if not bounds:
            raise LegacyExportError(f"Interval has no bounds: {expr!r}")
        condition = "(" + " and ".join(bounds) + ")"
    elif expr.startswith("MagicList(") and expr.endswith(")"):
        condition = f"({observed} == MagicList({value(expr[10:-1])}))"
    elif "," in expr and not _OPERATOR.match(expr):
        options = value(expr)
        condition = f"({observed} == MagicList({options}))"
    else:
        condition = compare(expr)
    check.condition = condition
    check.observed = observed
    if title != expr:
        check.expected += f" ( {expr} )"
    return check


def _call_names(step):
    raw = _normalize(step.get("subroutine"))
    if raw in _BLANK:
        return []
    return [part.strip() for part in raw.split(",")]


def _split_arguments(text):
    lines = [0]
    for match in re.finditer("\n", text):
        lines.append(match.end())
    start, depth = 0, 0
    parts = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type != tokenize.OP:
                continue
            if token.string in "([{":
                depth += 1
            elif token.string in ")]}":
                depth -= 1
            elif token.string == "," and depth == 0:
                offset = lines[token.start[0] - 1] + token.start[1]
                parts.append(text[start:offset].strip())
                start = offset + 1
    except tokenize.TokenError as exc:
        raise LegacyExportError(f"Invalid argument list: {text!r}") from exc
    return parts + [text[start:].strip()]


def _call_args(raw, multiple, names, signals, *, extend=False):
    if isinstance(raw, list):
        raw = ", ".join(map(str, raw))
    text = _normalize(raw)
    if text in _BLANK:
        if multiple:
            raise LegacyExportError("Parallel calls require a parenthesized argument group per call")
        return ["'*'" if extend and text == "*" else ""]
    if multiple:
        groups = _split_arguments(text)
        if any(not (group.startswith("(") and group.endswith(")")) for group in groups):
            raise LegacyExportError("Parallel calls require a parenthesized argument group per call")
        return [_call_args(group, False, names, signals)[0] for group in groups]
    if text.startswith("(") and text.endswith(")") and len(_split_arguments(text)) == 1:
        text = text[1:-1].strip()
    if not text:
        return [""]
    parts = _split_arguments(re.sub(r"(?<![,\n])\n\s*(?!$)", ", ", text))
    return [", ".join(_expression(part, names, signals) for part in parts if part)]


def _indent(lines, level):
    return ["    " * level + line for line in lines]


def _step_result(label, no, clock):
    passed = f"{label}Step.{no} is passed"
    failed = f"{label}Step.{no} is failed"
    return [
        "if isSuccessful:",
        f"    print({(passed + '.')!r})",
        f"    logging.info({(passed + ' at %ss.')!r}, {clock})",
        "else:",
        f"    print({(failed + '.')!r})",
        f"    logging.info({(failed + ' at %ss.')!r}, {clock})",
        f"    testResult = {no}",
        "    break",
    ]


def _procedure(body, field):
    steps = body.get("steps")
    if not isinstance(steps, list) or not steps or any(not isinstance(step, dict) for step in steps):
        raise LegacyExportError(f"{field}: a procedure with authored steps is required")
    return steps


def _render(name, body, constants, library, *, is_case=False, is_init=False, params=""):
    signals = Signals(body)
    statements, cleanup, pre_init, comments = [], [], [], []
    imports = {}
    steps = _procedure(body, name)
    seen = set()
    arguments = []
    params = _normalize(params).lstrip(":").strip()
    parameters = matrix._parse_lib_params("" if params in _BLANK else params)
    names = constants | {param for param, _ in parameters}
    for param, default in parameters:
        _identifier(param)
        arguments.append(param if default is None else
                         f"{param}={_expression(_normalize(default), constants)}")
    if is_init and len(steps) != 1:
        raise LegacyExportError("An initialization subroutine requires exactly one step")
    try:
        timeout = float(body.get("default_timeout", matrix.DEFAULT_TIMEOUT))
    except (TypeError, ValueError) as exc:
        raise LegacyExportError("Invalid default timeout") from exc
    if timeout < 0 or not math.isfinite(timeout):
        raise LegacyExportError("Invalid default timeout")
    label = "" if is_case else f"{name} "
    header_label = "" if is_case else f" Subroutine({name}) "
    tick = "yield" if is_case else "yield True"
    has_tbd = False
    for index, step in enumerate(steps):
        no = step.get("no")
        if not re.fullmatch(r"\d+", _text(no)):
            raise LegacyExportError(f"Invalid step number: {no!r}")
        no = int(no)
        if no in seen:
            raise LegacyExportError(f"Duplicate step number: {no}")
        seen.add(no)
        purpose, operation = _text(step.get("purpose")), _text(step.get("operation"))
        comments.append(operation)
        calls = _call_names(step)
        for call in calls:
            if call not in _BUILTINS:
                if call not in library:
                    raise LegacyExportError(f"Step {no}: undefined subroutine {call!r}")
                imports[call] = None
        hoisted = any(library.get(call, False) for call in calls)
        if hoisted and (not is_case or index != 0 or no != 1 or len(calls) != 1):
            raise LegacyExportError(f"Step {no}: initialization must be the first testcase call")
        inputs = list(_cells(step, "inputs", signals.inputs))
        checks = [_check(*cell, names, signals)
                  for cell in _cells(step, "expecteds", signals.expected)]
        if is_init and (calls or checks):
            raise LegacyExportError("Initialization subroutine steps may only set inputs")
        if hoisted and inputs:
            raise LegacyExportError(f"Step {no}: initialization call cannot share input assignments")
        if calls:
            args = _call_args(step.get("args"), len(calls) > 1, names, signals,
                              extend=calls == ["Extend_CompareNvramWith"])
            if len(calls) != len(args) or (len(calls) > 1 and "Extend_CompareNvramWith" in calls):
                raise LegacyExportError(f"Step {no}: invalid parallel subroutine calls")
            invocations = [f"{call}({arg})" for call, arg in zip(calls, args)]
            if hoisted:
                pre_init.extend([f"logging.info({purpose!r})",
                                 f"logging.info({(' ' + operation)!r})", invocations[0]])

        header = f"-------------------{header_label}Step{no}-------------------"
        lines = [f"testStepNo = {no}", "time_st = time.Value", f"print({header!r})"]
        if not hoisted:
            lines.extend([f"logging.info({header!r})", f"logging.info({purpose!r})",
                          f"logging.info({(header_label + f'Step{no} ' + operation)!r})"])
        for _, path, _, expr in inputs:
            if "T.B.D" in expr or "T.B.D" in path:
                has_tbd = True
                continue
            target = signals.bind(path)
            if not target.endswith(".Value"):
                raise LegacyExportError(f"Step {no}: cannot assign to {path!r}")
            lines.append(f"{target} = {_expression(expr, names, signals)}")
        extended = calls == ["Extend_CompareNvramWith"]
        if calls and not hoisted:
            if extended:
                lines.append(f"extended = {invocations[0]}")
            elif len(calls) > 1:
                lines.append(f"yield scheduler.Parallel({', '.join(invocations)})")
            else:
                lines.extend([f"gen = {invocations[0]}", "while next(gen):", f"    {tick}"])
        if not is_init:
            method, seconds, timing_label = _timing(step.get("timing"), timeout)
            conditions = [check.condition for check in checks if check.condition]
            has_tbd |= any(check.condition is None for check in checks)
            predicate = " and ".join(conditions) or "True"
            if extended:
                predicate += " and extended in ('Passed', 'Default')"
            lines.extend(["while True:", f"    isSuccessful = {predicate}"])
            boundary = "not isSuccessful" if method == matrix.JUDGE_KEEP else "isSuccessful"
            deadline = f"{seconds!r} - stepsize.Value" if method == matrix.JUDGE_KEEP else repr(seconds)
            lines.extend([f"    if {boundary} or round(time.Value - time_st, digit) >= {deadline}:",
                          "        break", f"    {tick}"])
            details = [line for check in checks for line in
                       [*check.logs(), f"logging.info({('    確認タイミング：' + timing_label)!r})"]]
            lines.extend(details)
            lines.extend(_step_result(label, no, "round(time.Value, digit)"))
            cleanup.extend([f"if testStepNo == {no}:", *_indent(details, 1),
                            f"    print('Step.{no} is failed.')",
                            f"    logging.info('Step.{no} is failed at %ss.', time)",
                            f"    logging.info('Test is failed in Step{no}!!!!')"])
        statements.extend(_indent(lines, 2))

    source = [PREAMBLE, LIB_PATHS.format(
        case_path=('Lib_in_path = os.path.normpath(os.path.join(base, "../../Lib/"))\n'
                   'sys.path.append(Lib_in_path)\n' if is_case else ""),
        root_path="../../../../.." if is_case else "../../../..",
    )]
    source.extend(f"from {call} import *" for call in imports)
    if is_case:
        source.extend(["", "logfile = sys.argv[1]",
                       "if os.path.exists(logfile):", "    os.remove(logfile)"])
    source.extend(["", "time = Variable('currentTime')",
                   "stepsize = Variable('modelStepSize')",
                   "digit = max(0, -Decimal(str(stepsize.Value)).as_tuple().exponent)"])
    source.extend(f"{variable} = Variable({path!r})" for path, variable in signals.variables.items())
    source.extend(["", f"cmt_g = {comments!r}"])
    if is_case:
        started = f"Test case ID.{name if name.startswith('ID;;') else 'ID;;' + name} is started!"
        source.extend(["testPythonOver = testStepNo = -1", "print(datetime.datetime.today())",
                       f"print({started!r})",
                       "logging.basicConfig(filename=logfile, encoding='UTF-8', level=logging.INFO)",
                       "logging.info(datetime.datetime.today())", f"logging.info({started!r})",
                       "", "def pre_init(time):",
                       "    logging.info('-------------------pre_init-------------------')",
                       *_indent(pre_init, 1), "    return DLL_OK", "",
                       "def MainGenerator(*args):"])
    else:
        source.extend(["", f"def {name}({', '.join(arguments)}):",
                       f"    logging.info({f' Subroutine({name}) is started!'!r})"])
    source.extend(["    global testPythonOver, testStepNo", "    testPythonOver = testStepNo = -1",
                   "    testResult = -1", "    while testResult == -1:",
                   *statements, "        break"])
    if is_case:
        passed_log = ("All steps are verified. Test is Passed. The test has T.B.D. judge value."
                      if has_tbd else "All steps are verified.Test is Passed.")
        source.extend([COMPLETION.format(passed=repr(passed_log)), "",
                       "def pre_cleanup(time):", "    if testPythonOver == 0:",
                       "        return DLL_OK", "    print('The test was suspended !!!')",
                       "    logging.info('The test was suspended !!!')",
                       *_indent(cleanup, 1), "    return DLL_OK"])
    elif not is_init:
        source.extend(["    testPythonOver = 0",
                       f"    logging.info({f' Subroutine({name}) is ended!'!r})",
                       "    yield False"])
    return "\n".join(source) + "\n", signals.labels


def build_legacy_zip(project_code, test_rows, const_rows, lib_rows):
    """Build the CSVLESS archive from saved project rows."""
    root = f"01_Spec_Report/{_segment(project_code)}/CSVLESS"
    const_source = ["# coding: UTF-8", "from CustomClass import *"]
    constants = set()
    for row in const_rows:
        name = _text(row.get_field(matrix.CONST_NAME))
        if not name:
            continue
        _identifier(name)
        if name in constants or name in _BUILTINS:
            raise LegacyExportError(f"Duplicate or reserved constant: {name}")
        raw = _normalize(row.get_field(matrix.CONST_VALUE))
        if re.match(r"^MagicList\(", raw) and raw.endswith(")"):
            value = f"MagicList({_expression(raw[10:-1], constants)})"
        else:
            value = _expression(raw or "0", constants)
        label = " ".join(_text(row.get_field(matrix.CONST_JNAME)).splitlines())
        const_source.append(f"{name} = {value}  # {label}".rstrip())
        constants.add(name)
    rows_by_name = {}
    for row in lib_rows:
        name = _text(row.get_field(matrix.LIB_FUNC) or row.get_field(matrix.LIB_NAME) or row.case_id)
        if name:
            rows_by_name.setdefault(name, []).append(row)
    kinds = {name: matrix._as_bool(rows[0].get_field(matrix.LIB_ISINIT))
             for name, rows in rows_by_name.items()}
    rendered_libs = {}

    def collect(body, labels, visited, visiting):
        for step in body["steps"]:
            for name in _call_names(step):
                if name in _BUILTINS or name in visited:
                    continue
                if name in visiting:
                    raise LegacyExportError(f"Circular subroutine reference: {name}")
                _identifier(name)
                rows = rows_by_name.get(name, [])
                if len(rows) != 1 or name in constants:
                    raise LegacyExportError(f"Missing or ambiguous subroutine: {name}")
                row = rows[0]
                sub_body = matrix._parse_json_field(row.get_field(matrix.LIB_STEPS))
                if name not in rendered_libs:
                    rendered_libs[name] = _render(
                        name, sub_body, constants, kinds, is_init=kinds[name],
                        params=row.get_field(matrix.LIB_PARA) or "")
                for path, label in rendered_libs[name][1].items():
                    labels.setdefault(path, label)
                collect(sub_body, labels, visited, visiting | {name})
                visited.add(name)

    files = {f"{root}/Lib/Constant.py": "\n".join(const_source) + "\n",
             "02_Config/Library/Lib/Common_Constant.py": "# coding: UTF-8\n"}
    files.update({f"02_Config/Library/StdLib/{name}.py": source for name, source in STDLIB.items()})
    case_dirs = set()
    for row in test_rows:
        test_id = matrix.row_test_id(row)
        body = matrix._parse_json_field(row.get_field(matrix.TEST_STEPS))
        try:
            folder = _segment(test_id)
            if folder.casefold() in case_dirs:
                raise LegacyExportError("Duplicate testcase directory")
            judge, labels = _render(test_id, body, constants, kinds, is_case=True)
            collect(body, labels, set(), set())
            prefix = f"{root}/TestCase/{folder}"
            files[f"{prefix}/judge.py"] = judge
            files[f"{prefix}/user.py"] = USER_PLOT.format(
                name=repr(test_id), signals=repr([(label, path) for path, label in labels.items()]))
            files[f"{prefix}/output.txt"] = "".join(path + "\r\n" for path in labels)
        except LegacyExportError as exc:
            raise LegacyExportError(f"{test_id}: {exc}") from exc
        case_dirs.add(folder.casefold())
    lib_files = {f"{root}/Lib/{name}.py": output[0] for name, output in rendered_libs.items()}
    paths = [path.casefold() for path in (*files, *lib_files)]
    if len(paths) != len(set(paths)):
        raise LegacyExportError("Duplicate library module name")
    files.update(lib_files)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, source in files.items():
            try:
                if path.endswith(".py"):
                    compile(source, path, "exec")
                    data = source.replace("\n", "\r\n").encode("utf-8-sig")
                else:
                    data = source.encode("cp932")
            except (SyntaxError, UnicodeError) as exc:
                raise LegacyExportError(f"{path}: {exc}") from exc
            archive.writestr(path, data)
    buffer.seek(0)
    return buffer
