import re
from mcp_server import mcp_app as app

# Helper to unwrap FastMCP tool objects (similar pattern used in other tests)
def _call(tool, *a, **kw):
    fn = getattr(tool, 'fn', None) or getattr(tool, '__wrapped__', None) or tool
    return fn(*a, **kw)

def test_fastpath_architecture_tool_returns_doc():
    data = _call(app.fastpath_architecture)
    assert isinstance(data, dict), 'Expected dict response'
    assert data.get('id') == 'concept:fastpath_architecture', 'Wrong doc id'
    assert data.get('text'), 'Missing concept text'
    # Basic sanity: ensure a few key phrases are present
    for phrase in ['Fast Path Architecture Overview', 'cycles_per_packet', 'busy_percent']:
        assert phrase in data['text'], f'Missing expected phrase {phrase}'


def test_system_prompt_mentions_fastpath_prefetch():
    prompt = app.SYSTEM_PROMPT
    assert 'fastpath_architecture' in prompt.lower(), 'System prompt does not mention fastpath_architecture tool'
    # Ensure guidance about calling it first is present
    pattern = re.compile(r'fastpath_architecture', re.IGNORECASE)
    assert pattern.search(prompt), 'Missing explicit fastpath_architecture reference'
