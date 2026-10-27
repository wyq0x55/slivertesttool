# Input repair context and diagnostics

`app.services.ut_pipeline.repair.revise` proposes appended input rows only.
Existing inputs, columns and expectations remain immutable. Accepted transport
records still require the existing typed witness checks, execution, recheck and
human review.

## Complete typed context

Prompt version `ut-repair/9.0` preserves every supplied repair-context field.
Repeated decisions and paths may use the lossless
`ut-platform.shared-repair-context/1` envelope:

- `decisions` contains supplied branch fields other than `required_outcome`.
- `paths` contains ordered edges with a zero-based `decision` index and the
  original `required_outcome` field, when present. A `literal` edge retains an
  unmodified non-object item.
- `records` contains each obligation's remaining fields in `record` and a
  zero-based `path` index. A record without an array path is retained unchanged
  inside `record`.

The renderer chooses the shorter of the complete original array and complete
shared envelope. It never reconstructs bindings or C semantics from names.
The evidence SHA-256 continues to bind the complete original evidence,
constraints, target, columns and input contract independently of rendering.
Changed evidence or prompt version rejects an old prefix snapshot.

If the complete context still exceeds 16,000 characters, `revise` returns
`PROMPT_BUDGET_EXCEEDED` before contacting the provider. Metadata reports the
required size, limit and `deferred_obligations`. All requested tasks are
deferred together; none becomes a model decline, claim or proof. The overall
96,000-character prompt limit also fails before provider contact.

## Response validation and retry feedback

The response must be a JSON object containing `rows` or `declined` (or both).
Omitted arrays default to empty; supplied arrays cannot be null or another
type. `rationale` and row `reason` are optional strings. Every row requires a
nonempty string obligation, an integer `base_row` (not a Boolean or numeric
string), and a nonempty input object mapping strings to nonempty strings.
Every decline requires a nonempty string obligation and reason. Unknown fields
are rejected. A malformed record invalidates the entire response before any
rows are applied.

Parse and schema failures return `Revision(None, meta)` with
`code=INVALID_MODEL_RESPONSE`, `error` and a bounded `reply_head`; schema errors
also carry structured `schema_errors` with JSON paths. Business validation of
well-formed rows still produces per-row `rejected` feedback through
`increment.apply`. The history renderer accepts bounded `error`, `reply_head`
and `schema_errors` fields for the next request.

## Optional job-local exchange capture

Call `revise(..., diagnostic_dir=Path(...))` with a new directory under the
owning job. The caller owns job authorization, containment and directory
selection. No default or global diagnostic directory is used.

- `request-messages.json` stores the actual system/user messages passed to the
  provider, serialized as UTF-8 JSON. Its bytes match `prompt_sha256`.
- `response.txt` stores the complete raw provider reply as UTF-8, including
  invalid JSON, schema failures, declines and successful proposals.
- `meta.artifact_root` is the supplied directory as a string.
- `meta.artifacts` uses existing Host descriptors: `id` is the first 16 hex
  characters of SHA-256 of the relative UTF-8 filename, `name` is that filename,
  and `sha256` binds the saved file bytes.

Files are created exclusively. Existing round directories are not overwritten.
Storage failure returns `DIAGNOSTIC_WRITE_FAILED` without a candidate. A
`ProviderError` continues to propagate without consuming a successful model
round; the saved request remains for caller-managed diagnosis and retry.
Artifact access must use the existing owning-job containment and hash checks.
Only messages and replies are captured; provider configuration and credentials
are not copied into these files. Private exchanges must remain local.
