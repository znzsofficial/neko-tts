"""Read bounded Focus membership from Host reply routing, not user messages."""


def planner_scopes(session_id, definitions):
    if isinstance(session_id, str) and session_id.strip():
        return (session_id,)
    if not isinstance(definitions, list) or len(definitions) > 512:
        return ()
    replies = [tool.get('function') for tool in definitions if isinstance(tool, dict)
               and tool.get('type') == 'function' and isinstance(tool.get('function'), dict)
               and tool['function'].get('name') == 'reply']
    if len(replies) != 1:
        return ()
    schema = replies[0].get('parameters')
    if not isinstance(schema, dict):
        return ()
    required, properties = schema.get('required'), schema.get('properties')
    if not isinstance(required, list) or 'session_id' not in required or not isinstance(properties, dict):
        return ()
    prop = properties.get('session_id')
    scopes = prop.get('enum') if isinstance(prop, dict) and prop.get('type') == 'string' else None
    if not isinstance(scopes, list) or not 0 < len(scopes) <= 32:
        return ()
    if any(not isinstance(s, str) or not 0 < len(s) <= 128 or s.strip() != s
           or any(ord(c) < 32 for c in s) for s in scopes):
        return ()
    return tuple(dict.fromkeys(scopes))
