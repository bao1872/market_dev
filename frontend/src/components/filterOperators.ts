export const FILTER_OPERATOR_ALIASES: Readonly<Record<string, string>> = {
  ne: 'neq',
  is_empty: 'empty',
  is_not_empty: 'not_empty',
  is_null: 'empty',
  is_not_null: 'not_empty',
  contains_any: 'has_any',
  contains_all: 'has_all',
  not_contains_any: 'not_has_any',
}

export function canonicalizeFilterOperator(operator: string): string {
  const normalized = operator.trim()
  return FILTER_OPERATOR_ALIASES[normalized] ?? normalized
}
