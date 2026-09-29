"""
Tests for Telegram HTML formatting module.
"""

import importlib.util
from pathlib import Path

# Load formatting module from hyphenated directory
plugin_path = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('formatting', plugin_path / 'html_render.py')
formatting = importlib.util.module_from_spec(spec)
spec.loader.exec_module(formatting)

render_chunks = formatting.render_chunks


def test_basic_bold_italic():
    """Test basic bold and italic formatting."""
    md = "**bold** and *italic* and ***both***"
    result = render_chunks(md)
    
    assert len(result) == 1
    assert result[0]['html'] == '<b>bold</b> and <i>italic</i> and <b><i>both</i></b>'
    assert result[0]['plain'] == 'bold and italic and both'


def test_inline_code():
    """Test inline code with special characters."""
    md = "Use `<script>&alert()</script>` carefully"
    result = render_chunks(md)
    
    assert len(result) == 1
    assert '<code>&lt;script&gt;&amp;alert()&lt;/script&gt;</code>' in result[0]['html']
    assert '<script>&alert()</script>' in result[0]['plain']


def test_fenced_code_block():
    """Test fenced code blocks with escaping."""
    md = """```python
def foo():
    return "<html>&test</html>"
```"""
    result = render_chunks(md)
    
    assert len(result) == 1
    assert '<pre>' in result[0]['html']
    assert '&lt;html&gt;&amp;test&lt;/html&gt;' in result[0]['html']
    assert '</pre>' in result[0]['html']
    assert '<html>&test</html>' in result[0]['plain']


def test_heading_to_bold():
    """Test headings convert to bold."""
    md = "# Heading One\n## Heading Two"
    result = render_chunks(md)
    
    assert len(result) == 1
    assert '<b>Heading One</b>' in result[0]['html']
    assert '<b>Heading Two</b>' in result[0]['html']


def test_bullet_list():
    """Test bullet list formatting."""
    md = "- First item\n- Second item\n* Third item"
    result = render_chunks(md)
    
    assert len(result) == 1
    assert '• First item' in result[0]['html']
    assert '• Second item' in result[0]['html']
    assert '• Third item' in result[0]['html']


def test_nested_bold_in_bullet():
    """Test bold formatting inside bullet points."""
    md = "- **Important:** this is a *key* point"
    result = render_chunks(md)
    
    assert len(result) == 1
    assert '• <b>Important:</b> this is a <i>key</i> point' in result[0]['html']
    assert '• Important: this is a key point' in result[0]['plain']


def test_safe_links():
    """Test safe link handling with http/https only."""
    md = "[Google](https://google.com) and [Bad](javascript:alert(1))"
    result = render_chunks(md)
    
    assert len(result) == 1
    assert '<a href="https://google.com">Google</a>' in result[0]['html']
    # Unsafe link stripped to text only
    assert 'Bad' in result[0]['html']
    assert 'javascript:alert(1)' not in result[0]['html']


def test_dangerous_link_schemes():
    """Test filtering of dangerous link schemes."""
    md = "[File](file:///etc/passwd) [Data](data:text/html,<script>) [FTP](ftp://example.com)"
    result = render_chunks(md)
    
    assert len(result) == 1
    html = result[0]['html']
    
    # All unsafe schemes stripped
    assert 'file://' not in html
    assert 'data:' not in html
    assert 'ftp://' not in html
    # But text preserved
    assert 'File' in html
    assert 'Data' in html
    assert 'FTP' in html


def test_markdown_table_conversion():
    """Test table conversion to bullet format."""
    md = """| Name | Value |
|------|-------|
| Key1 | Val1  |
| Key2 | Val2  |"""
    result = render_chunks(md)
    
    assert len(result) == 1
    html = result[0]['html']
    
    assert '• Name: Key1 · Value: Val1' in html
    assert '• Name: Key2 · Value: Val2' in html


def test_table_with_alignment_separators():
    """Test table with alignment markers in separator."""
    md = """| Left | Center | Right |
|:-----|:------:|------:|
| A    | B      | C     |"""
    result = render_chunks(md)
    
    assert len(result) == 1
    html = result[0]['html']
    
    # Should still parse correctly
    assert '• Left: A · Center: B · Right: C' in html


def test_long_content_splitting():
    """Test chunking at line boundaries for long content."""
    # Create content that exceeds default limit
    lines = [f"Line {i} with some text to make it longer" for i in range(200)]
    md = '\n'.join(lines)
    
    result = render_chunks(md, limit=1000)
    
    # Should be split into multiple chunks
    assert len(result) > 1
    
    # Each chunk should respect limit
    for chunk in result:
        html_len = len(chunk['html'].encode('utf-16-le')) // 2
        plain_len = len(chunk['plain'].encode('utf-16-le')) // 2
        assert html_len <= 1000, f"HTML chunk too long: {html_len}"
        assert plain_len <= 1000, f"Plain chunk too long: {plain_len}"


def test_emoji_and_utf16_entities():
    """Test UTF-16 length calculation with emoji and special chars."""
    # Emoji and special characters take multiple UTF-16 code units
    md = "🎉 " * 1000  # Each emoji is 2 UTF-16 units
    
    result = render_chunks(md, limit=500)
    
    # Should respect UTF-16 length limits
    # May be 1 chunk if fits, or split if needed
    assert len(result) >= 1
    
    for chunk in result:
        utf16_len = len(chunk['html'].encode('utf-16-le')) // 2
        assert utf16_len <= 500, f"Chunk over limit: {utf16_len}"


def test_code_block_splitting():
    """Test long code blocks split appropriately."""
    # Create a very long code block
    code_lines = [f"function_{i}();" for i in range(500)]
    md = "```javascript\n" + '\n'.join(code_lines) + "\n```"
    
    result = render_chunks(md, limit=2000)
    
    # Should handle long code blocks
    assert len(result) >= 1
    
    # Each chunk should have balanced tags
    for chunk in result:
        assert chunk['html'].count('<pre>') == chunk['html'].count('</pre>')


def test_empty_input():
    """Test handling of empty input."""
    result = render_chunks("")
    
    assert len(result) == 1
    assert result[0]['html'] == ''
    assert result[0]['plain'] == ''


def test_mixed_formatting():
    """Test complex mixed formatting."""
    md = """# Important Update

This is **bold** with *italic* and `code`.

- First **bold** bullet
- Second *italic* bullet

```python
print("hello")
```

| Col1 | Col2 |
|------|------|
| A    | B    |

[Link](https://example.com)"""
    
    result = render_chunks(md)
    
    assert len(result) >= 1
    html = '\n'.join(c['html'] for c in result)
    
    # Check all elements present
    assert '<b>Important Update</b>' in html
    assert '<code>code</code>' in html
    assert '• First <b>bold</b> bullet' in html
    assert '<pre>' in html
    assert '• Col1: A · Col2: B' in html
    assert '<a href="https://example.com">Link</a>' in html


def test_underline_variants():
    """Test both asterisk and underscore variants."""
    md = "__bold__ and _italic_ and **also bold** and *also italic*"
    result = render_chunks(md)
    
    assert len(result) == 1
    # Should convert both variants
    html = result[0]['html']
    assert html.count('<b>') == 2
    assert html.count('<i>') == 2


def test_special_html_chars_escaped():
    """Test HTML special characters are properly escaped."""
    md = "Test: <div>&nbsp;</div> and \"quotes\""
    result = render_chunks(md)
    
    # Raw HTML should be escaped (not rendered as tags)
    html = result[0]['html']
    # Should have escaped HTML entities
    assert '&lt;div&gt;' in html
    assert '&amp;nbsp;' in html
    assert '&lt;/div&gt;' in html


def test_chunk_balance():
    """Test chunks maintain reasonable balance."""
    md = '\n'.join([f"**Line {i}**" for i in range(100)])
    
    result = render_chunks(md, limit=500)
    
    # All chunks should have content
    for chunk in result:
        assert len(chunk['html']) > 0
        assert len(chunk['plain']) > 0
        # Tags should be balanced (count <b> == </b>)
        assert chunk['html'].count('<b>') == chunk['html'].count('</b>')
