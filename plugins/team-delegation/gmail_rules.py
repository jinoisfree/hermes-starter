"""Pure validation shared by Gmail previews and the subprocess; never calls Gmail."""

CRITERIA_FIELDS = frozenset({"from", "to", "subject", "query", "negatedQuery", "hasAttachment"})
RULE_FIELDS = frozenset({"label_name", "criteria", "skip_inbox", "mark_read", "apply_existing"})


class RuleError(ValueError):
    """Safe code with an optional one-based rule index, without caller contents."""

    def __init__(self, code, rule_index=None):
        super().__init__(code)
        self.rule_index = rule_index


def required_text(request, field):
    value = request.get(field)
    if not isinstance(value, str) or not value.strip():
        raise RuleError("GMAIL_" + field.upper() + "_REQUIRED")
    return value


def boolean_fields(request, *fields):
    for field in fields:
        if field in request and not isinstance(request[field], bool):
            raise RuleError("GMAIL_" + field.upper() + "_REQUIRED")


def validate_criteria(value):
    if not isinstance(value, dict) or not value:
        raise RuleError("GMAIL_CRITERIA_REQUIRED")
    if set(value) - CRITERIA_FIELDS:
        raise RuleError("GMAIL_FILTER_CRITERIA_INVALID")
    for field, item in value.items():
        if field == "hasAttachment":
            valid = isinstance(item, bool)
        else:
            valid = isinstance(item, str) and bool(item.strip())
        if not valid:
            raise RuleError("GMAIL_FILTER_CRITERIA_INVALID")


def validate_rules(rules):
    if not isinstance(rules, list) or len(rules) > 10:
        raise RuleError("GMAIL_RULES_REQUIRED")
    for index, rule in enumerate(rules, 1):
        try:
            if not isinstance(rule, dict) or set(rule) - RULE_FIELDS:
                raise RuleError("GMAIL_RULE_INVALID")
            required_text(rule, "label_name")
            validate_criteria(rule.get("criteria"))
            boolean_fields(rule, "skip_inbox", "mark_read", "apply_existing")
        except RuleError as exc:
            raise RuleError(str(exc), index) from None


def optional_strings(request, field):
    values = request.get(field, [])
    if (not isinstance(values, list) or len(values) > 5
            or any(not isinstance(value, str) or not value.strip() for value in values)):
        raise RuleError("GMAIL_" + field.upper() + "_REQUIRED")


def validate_operation(action, request):
    # Do not insert defaults or normalize values: the approved args must stay identical.
    boolean_fields(request, "skip_inbox", "mark_read", "apply_existing")
    if action == "label_create":
        required_text(request, "label_name")
    elif action == "label_apply":
        required_text(request, "query")
        required_text(request, "label_id")
    elif action == "filter_create":
        validate_criteria(request.get("criteria"))
        required_text(request, "label_id")
    elif action == "filter_delete":
        required_text(request, "filter_id")
    elif action == "mail_organize":
        validate_rules(request.get("rules", []))
        optional_strings(request, "delete_filter_ids")
        optional_strings(request, "restore_inbox_queries")
        if not any(request.get(field) for field in ("rules", "delete_filter_ids", "restore_inbox_queries")):
            raise RuleError("GMAIL_RULES_REQUIRED")
    elif action == "inbox_restore":
        required_text(request, "query")


def criteria_query(criteria):
    """Group each term so all criteria, including an OR query, must match."""
    validate_criteria(criteria)
    terms = [f"{field}:({criteria[field]})" for field in ("from", "to", "subject") if field in criteria]
    if "query" in criteria:
        terms.append("(" + criteria["query"] + ")")
    if "negatedQuery" in criteria:
        terms.append("-(" + criteria["negatedQuery"] + ")")
    if criteria.get("hasAttachment"):
        terms.append("has:attachment")
    # False leaves the API's attachment requirement disabled; use negatedQuery to exclude attachments.
    return " ".join(terms)
