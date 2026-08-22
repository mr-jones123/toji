;; TS/JS extraction queries (shared: patterns for TS-only node types never match JS trees)

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

;; class field holding an arrow/function closure: `field = () => ...`
(public_field_definition
  name: (property_identifier) @name
  value: (arrow_function)
) @method
(public_field_definition
  name: (property_identifier) @name
  value: (function_expression)
) @method

(class_declaration
  name: (type_identifier) @name
  (class_heritage (extends_clause value: (_) @base))?
) @class

(abstract_class_declaration
  name: (type_identifier) @name
  (class_heritage (extends_clause value: (_) @base))?
) @class

(interface_declaration
  name: (type_identifier) @name
  (extends_type_clause (type_identifier) @base)?
) @interface

(type_alias_declaration
  name: (type_identifier) @name
) @type

(enum_declaration
  name: (identifier) @name
) @enum

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
