/*
 * SBS editor bundle: CodeMirror 6 with the user's own VS Code TextMate grammar.
 *
 * Vite compiles this (with CodeMirror 6 + vscode-textmate + vscode-oniguruma
 * inlined) into
 *   app/static/vendor/sbs/sbs-editor.umd.js (+ .css)
 * and it assigns `window.LMSbsEditor = { mount }`.
 *
 * The grammar (`sbsV202403.tmLanguage.json`) and the Oniguruma WASM regex
 * engine are NOT bundled -- they are fetched at runtime from URLs the caller
 * passes in (served from app/static/vendor/sbs/ via Flask `url_for`). This
 * keeps the build decoupled from the serving path and lets the user drop in an
 * updated grammar without rebuilding.
 *
 * Highlighting is faithful to the VS Code extension: vscode-textmate tokenizes
 * each line with the real `.tmLanguage.json`, and each token's scope stack is
 * mapped to a small set of CSS classes themed below. Unknown external includes
 * (source.arm/asm/x86*) degrade gracefully (no sub-highlight).
 *
 * All history / save / lock logic lives in the build-free controller
 * app/static/js/lanmatrix/sbs_editor.js; this bundle only ships the editor.
 * If the bundle is absent the controller falls back to a plain <textarea>.
 */
import {
  EditorState, StateField, Compartment, RangeSetBuilder, EditorSelection,
} from "@codemirror/state";
import {
  EditorView, Decoration, keymap, lineNumbers, highlightActiveLine,
  highlightActiveLineGutter, drawSelection, dropCursor, rectangularSelection,
  highlightSpecialChars,
} from "@codemirror/view";
import {
  defaultKeymap, history, historyKeymap, indentWithTab,
} from "@codemirror/commands";
import { bracketMatching, indentOnInput } from "@codemirror/language";
import { closeBrackets, closeBracketsKeymap } from "@codemirror/autocomplete";
import { searchKeymap, highlightSelectionMatches } from "@codemirror/search";

import { Registry, parseRawGrammar, INITIAL } from "vscode-textmate";
import * as oniguruma from "vscode-oniguruma";

const SCOPE_NAME = "sourceV202403.sbs";

/* ---- TextMate scope -> highlight class -------------------------------- */
// Ordered longest/most-specific first. The token's full scope-stack string is
// tested against each prefix; first hit wins.
const SCOPE_RULES: Array<[string, string]> = [
  ["comment", "tm-comment"],
  ["string", "tm-string"],
  ["constant.numeric", "tm-number"],
  ["constant.character", "tm-number"],
  ["constant.language", "tm-constant"],
  ["support.constant", "tm-constant"],
  ["constant", "tm-constant"],
  ["keyword.operator", "tm-operator"],
  ["keyword.control.directive", "tm-preproc"],
  ["meta.preprocessor", "tm-preproc"],
  ["punctuation.definition.directive", "tm-preproc"],
  ["storage", "tm-keyword"],
  ["keyword", "tm-keyword"],
  ["entity.name.function", "tm-function"],
  ["support.function", "tm-function"],
  ["entity.name.type", "tm-type"],
  ["support.type", "tm-type"],
  ["variable.parameter", "tm-param"],
  ["variable", "tm-variable"],
  ["support.variable", "tm-variable"],
  ["entity.name", "tm-type"],
  ["invalid", "tm-invalid"],
];

function scopesToClass(scopes: string[]): string | null {
  // Most specific scope is last; test from the end for a category match.
  const joined = scopes.join(" ");
  for (const [prefix, cls] of SCOPE_RULES) {
    if (joined.indexOf(prefix) !== -1) { return cls; }
  }
  return null;
}

/* ---- Registry / grammar loading (cached by URL pair) ------------------ */
let _wasmReady: Promise<void> | null = null;
const _grammarCache: Record<string, Promise<any>> = {};

function loadWasm(wasmUrl: string): Promise<void> {
  if (!_wasmReady) {
    _wasmReady = fetch(wasmUrl)
      .then((r) => r.arrayBuffer())
      .then((buf) => oniguruma.loadWASM(buf) as unknown as Promise<void>);
  }
  return _wasmReady;
}

function loadGrammar(grammarUrl: string, wasmUrl: string): Promise<any> {
  const key = grammarUrl + "|" + wasmUrl;
  if (!_grammarCache[key]) {
    _grammarCache[key] = (async () => {
      await loadWasm(wasmUrl);
      const grammarText = await fetch(grammarUrl).then((r) => r.text());
      const registry = new Registry({
        onigLib: Promise.resolve({
          createOnigScanner: (patterns: string[]) =>
            new (oniguruma as any).OnigScanner(patterns),
          createOnigString: (s: string) =>
            new (oniguruma as any).OnigString(s),
        }),
        loadGrammar: async (scopeName: string) => {
          if (scopeName === SCOPE_NAME) {
            return parseRawGrammar(grammarText, "sbs.tmLanguage.json");
          }
          return null; // external includes (source.arm/asm/...) not shipped
        },
      });
      return registry.loadGrammar(SCOPE_NAME);
    })();
  }
  return _grammarCache[key];
}

/* ---- Decorations: tokenize the whole (small) doc on each change -------- */
function buildDecorations(state: EditorState, grammar: any) {
  const builder = new RangeSetBuilder<any>();
  if (!grammar) { return builder.finish(); }
  let ruleStack = INITIAL;
  const doc = state.doc;
  for (let n = 1; n <= doc.lines; n++) {
    const line = doc.line(n);
    let res: any;
    try {
      res = grammar.tokenizeLine(line.text, ruleStack);
    } catch (_e) {
      break; // never let a tokenizer hiccup kill the editor
    }
    ruleStack = res.ruleStack;
    for (const tok of res.tokens) {
      const cls = scopesToClass(tok.scopes);
      if (!cls) { continue; }
      const from = line.from + tok.startIndex;
      const to = line.from + tok.endIndex;
      if (to > from) {
        builder.add(from, to, Decoration.mark({ class: cls }));
      }
    }
  }
  return builder.finish();
}

function tmField(grammar: any) {
  return StateField.define<any>({
    create(state) { return buildDecorations(state, grammar); },
    update(deco, tr) {
      if (tr.docChanged) { return buildDecorations(tr.state, grammar); }
      return deco;
    },
    provide: (f) => EditorView.decorations.from(f),
  });
}

/* ---- Theme ------------------------------------------------------------ */
const baseTheme = EditorView.theme({
  "&": { height: "100%", fontSize: "13px" },
  ".cm-scroller": {
    fontFamily:
      "ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace",
    lineHeight: "1.5",
  },
  ".cm-content": { caretColor: "#111" },
  "&.cm-focused .cm-cursor": { borderLeftColor: "#111" },
  ".tm-comment": { color: "#6a737d", fontStyle: "italic" },
  ".tm-string": { color: "#032f62" },
  ".tm-number": { color: "#005cc5" },
  ".tm-constant": { color: "#005cc5" },
  ".tm-keyword": { color: "#d73a49" },
  ".tm-operator": { color: "#d73a49" },
  ".tm-preproc": { color: "#6f42c1" },
  ".tm-function": { color: "#6f42c1" },
  ".tm-type": { color: "#22863a" },
  ".tm-variable": { color: "#e36209" },
  ".tm-param": { color: "#24292e" },
  ".tm-invalid": { color: "#b31d28", textDecoration: "underline wavy #b31d28" },
});

export interface MountOptions {
  container: HTMLElement;
  doc?: string;
  wasmUrl: string;
  grammarUrl: string;
  readOnly?: boolean;
  onChange?: (value: string) => void;
}

export interface EditorHandle {
  getValue(): string;
  setValue(text: string): void;
  setReadOnly(ro: boolean): void;
  focus(): void;
  destroy(): void;
}

const readOnlyCompartment = new Compartment();

export async function mount(opts: MountOptions): Promise<EditorHandle> {
  const grammar = await loadGrammar(opts.grammarUrl, opts.wasmUrl);

  const listeners = EditorView.updateListener.of((u) => {
    if (u.docChanged && opts.onChange) {
      opts.onChange(u.state.doc.toString());
    }
  });

  const state = EditorState.create({
    doc: opts.doc || "",
    extensions: [
      lineNumbers(),
      highlightActiveLineGutter(),
      highlightSpecialChars(),
      history(),
      drawSelection(),
      dropCursor(),
      indentOnInput(),
      bracketMatching(),
      closeBrackets(),
      rectangularSelection(),
      highlightActiveLine(),
      highlightSelectionMatches(),
      keymap.of([
        ...closeBracketsKeymap,
        ...defaultKeymap,
        ...searchKeymap,
        ...historyKeymap,
        indentWithTab,
      ]),
      tmField(grammar),
      baseTheme,
      readOnlyCompartment.of(EditorState.readOnly.of(!!opts.readOnly)),
      EditorView.editable.of(!opts.readOnly),
      listeners,
    ],
  });

  const view = new EditorView({ state, parent: opts.container });

  return {
    getValue: () => view.state.doc.toString(),
    setValue: (text: string) => {
      view.dispatch({
        changes: { from: 0, to: view.state.doc.length, insert: text || "" },
        selection: EditorSelection.single(0),
      });
    },
    setReadOnly: (ro: boolean) => {
      view.dispatch({
        effects: readOnlyCompartment.reconfigure(
          EditorState.readOnly.of(ro)),
      });
    },
    focus: () => view.focus(),
    destroy: () => view.destroy(),
  };
}

declare global {
  interface Window {
    LMSbsEditor?: { mount: typeof mount };
  }
}

window.LMSbsEditor = { mount };
