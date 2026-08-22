;; JavaScript extraction queries (separate from tsjs.scm: JS grammar lacks
;; type_identifier, public_field_definition, interface/type/enum declarations)

(function_declaration
  name: (identifier) @name
  parameters: (formal_parameters) @params
) @func

(generator_function_declaration
  name: (identifier) @name
  parameters: (formal_parameters) @params
) @func

(method_definition
  name: (property_identifier) @name
  parameters: (formal_parameters) @params
) @method

(class_declaration
  name: (identifier) @name
  (class_heritage (identifier) @base)?
) @class

;; `const foo = (x) => ...` / `const foo = function ...`
(variable_declarator
  name: (identifier) @name
  value: (arrow_function parameters: (formal_parameters) @params)
) @varfn
(variable_declarator
  name: (identifier) @name
  value: (function_expression parameters: (formal_parameters) @params)
) @varfn

;; imports + re-exports
(import_statement
  source: (string) @src
) @imp
(export_statement
  source: (string) @src
) @imp

;; calls — callee is the whole function expression
(call_expression
  function: (_) @callee
) @call
