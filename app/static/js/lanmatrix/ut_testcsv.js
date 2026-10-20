/*
 * ut_testcsv.js — window.LMTestCsv: a WinAMS TestCsv seen as a step table.
 *
 * The step-detail drawer (steps_editor.js) edits a columnar document of input
 * signals, expected signals and steps. A TestCsv maps onto it directly:
 *   mod line, 4th / 5th field → input / output column counts
 *   #COMMENT header           → the signal columns (shown, never edited: the
 *                               generator owns the column roles)
 *   ";…" lines                → the comment of the data row that follows
 *   ",v1,v2,…" lines          → one step: input values, then expected outputs
 * Everything the table does not show (the mod / % preamble, other lines,
 * trailing comments, result columns after the outputs) is carried through
 * unchanged, and an unchanged row keeps its original text, so serialising an
 * unedited table returns the original CSV.
 */
(function (window) {
  "use strict";

  var LABEL = ";$L$,";
  // Each comment has its own full-width sheet row before the related values.
  // The canonical document still has one step per case for CSV round-tripping.
  var LAYOUT = { left: [], right: [], signalSheets: false, commentField: "purpose",
                 autoRowHeight: false, freezeLeft: false, textCells: true };
  var MISMATCH = "#f9c9c9";

  function splitLine(line) {
    var fields = [], cur = "", quoted = false;
    for (var i = 0; i < line.length; i++) {
      var ch = line[i];
      if (quoted) {
        if (ch === '"' && line[i + 1] === '"') { cur += '"'; i++; }
        else if (ch === '"') quoted = false;
        else cur += ch;
      } else if (ch === '"') quoted = true;
      else if (ch === ",") { fields.push(cur); cur = ""; }
      else cur += ch;
    }
    fields.push(cur);
    return fields;
  }
  function joinLine(fields) {
    return fields.map(function (v) {
      v = v == null ? "" : String(v);
      return /[",\r\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
    }).join(",");
  }

  /* Parse CSV text; null when it is not a TestCsv (no #COMMENT header). */
  function parse(text) {
    var lines = String(text || "").replace(/\r\n?/g, "\n").split("\n");
    if (lines.length && lines[lines.length - 1] === "") lines.pop();
    var at = -1;
    for (var i = 0; i < lines.length; i++) {
      if (lines[i].indexOf("#COMMENT") === 0) { at = i; break; }
    }
    if (at < 0) return null;
    var columns = splitLine(lines[at]).slice(1);
    var mod = null;
    for (var j = 0; j < at; j++) if (lines[j].indexOf("mod,") === 0) { mod = splitLine(lines[j]); break; }
    var ni = mod ? parseInt(mod[3], 10) : NaN;
    var no = mod ? parseInt(mod[4], 10) : NaN;
    // Without usable counts every column is shown as an input.
    if (!(ni >= 0 && no >= 0 && ni + no <= columns.length)) { ni = columns.length; no = 0; }
    var rows = [], pending = [];
    lines.slice(at + 1).forEach(function (line) {
      if (line.charAt(0) === ",") {
        rows.push({ lead: pending, raw: line, fields: splitLine(line).slice(1) });
        pending = [];
      } else pending.push(line);
    });
    return { preamble: lines.slice(0, at + 1), columns: columns, ni: ni, no: no,
             rows: rows, tail: pending };
  }

  function commentOf(lead) {
    return lead.filter(function (line) { return line.charAt(0) === ";"; }).map(function (line) {
      return line.indexOf(LABEL) === 0 ? line.slice(LABEL.length) : line;
    }).join("\n");
  }
  // A column header without its "file.c/" scope, for a narrow table header.
  function shortName(column) {
    var slash = column.indexOf("/");
    return slash >= 0 ? column.slice(slash + 1) : column;
  }

  /* The step document the drawer renders. */
  function toDoc(model) {
    function signal(column) { return [shortName(column), column]; }
    return {
      input_signals: model.columns.slice(0, model.ni).map(signal),
      expected_signals: model.columns.slice(model.ni, model.ni + model.no).map(signal),
      steps: model.rows.map(function (row, i) {
        return {
          no: i + 1, purpose: commentOf(row.lead), operation: "", subroutine: "", args: "",
          inputs: row.fields.slice(0, model.ni),
          expecteds: row.fields.slice(model.ni, model.ni + model.no), timing: "",
        };
      }),
    };
  }

  // A sheet may hand back a typed value; TestCsv text spells booleans upper-case.
  function text(v) {
    if (v === true) return "TRUE";
    if (v === false) return "FALSE";
    return v == null ? "" : String(v);
  }
  function cells(list, n) {
    var out = [];
    for (var i = 0; i < n; i++) out.push(text(list && list[i]));
    return out;
  }

  /* CSV text for an edited document; rows follow the document order. */
  function serialize(model, doc) {
    var out = model.preamble.slice();
    (doc.steps || []).forEach(function (step, i) {
      var orig = model.rows[i];
      var comment = text(step.purpose);
      if (orig && comment === commentOf(orig.lead)) out.push.apply(out, orig.lead);
      else {
        if (orig) out.push.apply(out, orig.lead.filter(function (l) { return l.charAt(0) !== ";"; }));
        comment.split("\n").forEach(function (line) {
          if (line.trim()) out.push(line.charAt(0) === ";" ? line : LABEL + line);
        });
      }
      var values = cells(step.inputs, model.ni).concat(cells(step.expecteds, model.no));
      var before = orig ? cells(orig.fields, model.ni + model.no) : null;
      if (before && before.join("\u0000") === values.join("\u0000")) out.push(orig.raw);
      else out.push(joinLine([""].concat(values, orig ? orig.fields.slice(model.ni + model.no) : [])));
    });
    out.push.apply(out, model.tail);
    return out.join("\r\n") + "\r\n";
  }

  /* Cells the checker disputes (ORACLE_MISMATCH: 0-based data row + output column). */
  function marks(model, check) {
    var outputs = model.columns.slice(model.ni, model.ni + model.no);
    var found = [];
    ((check && check.issues) || []).forEach(function (issue) {
      if (issue.code !== "ORACLE_MISMATCH" || !Array.isArray(issue.detail)) return;
      issue.detail.forEach(function (d) {
        var index = d ? outputs.indexOf(d.column) : -1;
        if (index >= 0 && typeof d.row === "number") {
          found.push({ step: d.row, kind: "expected", index: index, color: MISMATCH });
        }
      });
    });
    return found;
  }

  window.LMTestCsv = { LAYOUT: LAYOUT, parse: parse, toDoc: toDoc, serialize: serialize,
                       marks: marks, shortName: shortName };
})(window);
