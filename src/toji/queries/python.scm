;; Python extraction queries
;; Defs with docstring (pattern 0) and without (pattern 1) — matches are deduped by node id.

(function_definition
  name: (identifier) @name
  parameters: (parameters) @params
  body: (block . (expression_statement (string) @docstring))
) @func

(function_definition
  name: (identifier) @name
  parameters: (parameters) @params
) @func

(class_definition
  name: (identifier) @name
  body: (block . (expression_statement (string) @docstring))
) @class

(class_definition
  name: (identifier) @name
) @class

;; Module docstring
(module . (expression_statement (string) @docstring)) @mod_doc

;; Calls — callee is the whole function expression (identifier, attribute, chained)
(call function: (_) @callee) @call

;; Imports — whole statements; names are walked in the extractor (handles
;; multi-name, multiline, aliased, and wildcard imports uniformly)
(import_statement) @imp
(import_from_statement) @imp
