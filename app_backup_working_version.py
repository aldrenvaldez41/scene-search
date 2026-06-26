from flask import Flask, request, render_template_string
import sqlite3
from html import escape
from collections import defaultdict
import re

DB_FILE = "subtitles.db"

app = Flask(__name__)

HTML = """
<h1>Subtitle Search</h1>
<!-- Include your HTML template here -->
"""


def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def seconds_to_timestamp(seconds):
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60

    if h:
        return f"{h:02}:{m:02}:{s:02}"

    return f"{m:02}:{s:02}"


def tokenize_query(query):
    """Tokenize query while respecting quoted phrases and parentheses."""
    tokens = []
    current = ""
    in_quotes = False
    in_parens = 0
    
    i = 0
    while i < len(query):
        char = query[i]
        
        if char == '"' and (i == 0 or query[i-1] != '\\'):
            in_quotes = not in_quotes
            current += char
        elif char == '(' and not in_quotes:
            if current.strip():
                tokens.append(current.strip())
                current = ""
            in_parens += 1
            current += char
        elif char == ')' and not in_quotes:
            in_parens -= 1
            current += char
            if in_parens == 0:
                tokens.append(current.strip())
                current = ""
        elif char in (' ', '\t') and not in_quotes and in_parens == 0:
            if current.strip():
                tokens.append(current.strip())
            current = ""
        else:
            current += char
        
        i += 1
    
    if current.strip():
        tokens.append(current.strip())
    
    return tokens


def parse_expression(expr):
    """
    Parse a single expression which can be:
    - A simple term: "word", "-word", "phrase"
    - OR expression: term1 | term2 | term3
    - Grouped expression: (expr)
    - Proximity: "word1 word2"~N
    - NEAR: term1 NEAR/N term2
    - NOTNEAR: term1 NOTNEAR/N term2
    """
    expr = expr.strip()
    
    # Handle grouped expressions: (expr)
    if expr.startswith('(') and expr.endswith(')'):
        return parse_expression(expr[1:-1])
    
    # Check for NEAR/NOTNEAR operators (highest precedence)
    near_match = re.match(r'(.+?)\s+(NEAR|NOTNEAR)/(\d+)\s+(.+)', expr, re.IGNORECASE)
    if near_match:
        left = near_match.group(1)
        op = near_match.group(2).upper()
        distance = int(near_match.group(3))
        right = near_match.group(4)
        return {
            'type': op,
            'left': parse_expression(left),
            'right': parse_expression(right),
            'distance': distance
        }
    
    # Check for OR operator
    if '|' in expr:
        parts = [p.strip() for p in expr.split('|')]
        return {
            'type': 'OR',
            'terms': [parse_expression(p) for p in parts]
        }
    
    # Check for proximity operator: "phrase"~N
    prox_match = re.match(r'^"([^"]+)"~(\d+)$', expr)
    if prox_match:
        phrase = prox_match.group(1)
        distance = int(prox_match.group(2))
        return {
            'type': 'PROXIMITY',
            'phrase': phrase,
            'distance': distance
        }
    
    # Check for exclusion: -word
    if expr.startswith('-'):
        return {
            'type': 'NOT',
            'term': expr[1:].strip()
        }
    
    # Check for exact phrase: "phrase"
    if expr.startswith('"') and expr.endswith('"'):
        return {
            'type': 'PHRASE',
            'phrase': expr[1:-1]
        }
    
    # Simple term
    return {
        'type': 'TERM',
        'term': expr
    }


def build_sql_from_ast(ast):
    """
    Convert parsed AST to SQL WHERE clause and parameters.
    Returns (where_clause, params)
    """
    if ast['type'] == 'TERM':
        return ("LOWER(s.subtitle_text) LIKE LOWER(?)", [f"%{ast['term']}%"])
    
    elif ast['type'] == 'PHRASE':
        return ("LOWER(s.subtitle_text) LIKE LOWER(?)", [f"%{ast['phrase']}%"])
    
    elif ast['type'] == 'NOT':
        return ("LOWER(s.subtitle_text) NOT LIKE LOWER(?)", [f"%{ast['term']}%"])
    
    elif ast['type'] == 'PROXIMITY':
        # Proximity: words within N words of each other
        # Split the phrase and create a pattern with word boundaries
        words = ast['phrase'].split()
        if len(words) < 2:
            return ("LOWER(s.subtitle_text) LIKE LOWER(?)", [f"%{ast['phrase']}%"])
        
        # For now, use simple substring matching (advanced PROXIMITY would need FTS)
        # This is a simplified implementation
        conditions = []
        params = []
        for word in words:
            conditions.append("LOWER(s.subtitle_text) LIKE LOWER(?)")
            params.append(f"%{word}%")
        
        return (" AND ".join(conditions), params)
    
    elif ast['type'] == 'OR':
        or_conditions = []
        all_params = []
        
        for term in ast['terms']:
            condition, params = build_sql_from_ast(term)
            or_conditions.append(f"({condition})")
            all_params.extend(params)
        
        return ("(" + " OR ".join(or_conditions) + ")", all_params)
    
    elif ast['type'] == 'NEAR':
        # NEAR: two terms within N words (simplified using AND + word count heuristic)
        left_sql, left_params = build_sql_from_ast(ast['left'])
        right_sql, right_params = build_sql_from_ast(ast['right'])
        
        # Simplified: require both terms to appear
        combined_sql = f"({left_sql} AND {right_sql})"
        combined_params = left_params + right_params
        
        return (combined_sql, combined_params)
    
    elif ast['type'] == 'NOTNEAR':
        # NOTNEAR: first term appears but not within N words of second (simplified)
        left_sql, left_params = build_sql_from_ast(ast['left'])
        right_sql, right_params = build_sql_from_ast(ast['right'])
        
        # Simplified: first term appears but second doesn't
        combined_sql = f"({left_sql} AND NOT {right_sql})"
        combined_params = left_params + right_params
        
        return (combined_sql, combined_params)
    
    return ("1=1", [])


def build_sql_from_query(query):
    """Main entry point for query parsing."""
    query = query.strip()
    
    if not query:
        return "1=1", []
    
    # Tokenize the top-level query
    tokens = tokenize_query(query)
    
    # Parse each token and combine with AND
    conditions = []
    all_params = []
    
    for token in tokens:
        ast = parse_expression(token)
        sql, params = build_sql_from_ast(ast)
        conditions.append(f"({sql})")
        all_params.extend(params)
    
    where_clause = " AND ".join(conditions) if conditions else "1=1"
    
    return where_clause, all_params


def highlight_text(text, query):
    """Highlight matching terms in text."""
    escaped = escape(text)
    
    # Extract all searchable terms from query
    terms = set()
    
    # Extract quoted phrases
    terms.update(re.findall(r'"([^"]+)"', query))
    
    # Remove quoted phrases and NEAR/NOTNEAR operators from query
    query_clean = re.sub(r'"[^"]+"', '', query)
    query_clean = re.sub(r'\bNEAR/\d+\b', '', query_clean, flags=re.IGNORECASE)
    query_clean = re.sub(r'\bNOTNEAR/\d+\b', '', query_clean, flags=re.IGNORECASE)
    query_clean = re.sub(r'~\d+', '', query_clean)
    query_clean = re.sub(r'[()|\-]', ' ', query_clean)
    
    # Extract remaining terms
    terms.update(t.strip() for t in query_clean.split() if t.strip())
    
    # Highlight terms (longest first to avoid partial overlaps)
    for term in sorted(terms, key=len, reverse=True):
        if term:
            pattern = re.compile(re.escape(term), re.IGNORECASE)
            escaped = pattern.sub(lambda m: f"<mark>{m.group()}</mark>", escaped)
    
    return escaped


@app.route("/")
def search():
    q = request.args.get("q", "").strip()
    videos = []

    if q:
        conn = get_db()
        
        where_clause, params = build_sql_from_query(q)
        
        sql = f"""
            SELECT
                s.video_id,
                s.start_seconds,
                s.subtitle_text,
                v.channel_name
            FROM subtitle_segments s
            JOIN videos v
                ON s.video_id=v.video_id
            WHERE {where_clause}
            ORDER BY s.video_id, s.start_seconds
            LIMIT 3000
        """
        
        try:
            results = conn.execute(sql, params).fetchall()
        except Exception as e:
            print(f"Database error: {e}")
            results = []
        finally:
            conn.close()
        
        grouped = defaultdict(list)
        
        for row in results:
            grouped[row["video_id"]].append(row)
        
        for video_id, rows in grouped.items():
            matches = []
            
            for row in rows[:8]:
                matches.append({
                    "seconds": int(row["start_seconds"]),
                    "timestamp": seconds_to_timestamp(row["start_seconds"]),
                    "text": highlight_text(row["subtitle_text"], q)
                })
            
            videos.append({
                "video_id": video_id,
                "channel_name": rows[0]["channel_name"],
                "matches": matches
            })

    return render_template_string(
        HTML,
        query=q,
        videos=videos,
        video_count=len(videos)
    )


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True
    )
