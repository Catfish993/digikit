//! Canonical JSON: the bytes of Python's `json.dumps(obj, sort_keys=True, indent=1, ensure_ascii=True) + "\n"`.
//!
//! Committed `.dko.json` files are compared as text, so the layout must match exactly: one space of indent per level, `": "`
//! after a key, `","` before the newline of the next item, `[]` and `{}` for empty containers, keys in code point order,
//! every non-ASCII character as `\uXXXX` (surrogate pairs above U+FFFF), lower case hex.

use serde_json::Value;
use std::fmt::Write;

pub fn to_string(v: &Value) -> String {
    let mut s = String::new();
    write_value(&mut s, v, 0);
    s.push('\n');
    s
}

fn indent(s: &mut String, level: usize) {
    s.push('\n');
    for _ in 0..level {
        s.push(' ');
    }
}

fn write_value(s: &mut String, v: &Value, level: usize) {
    match v {
        Value::Null => s.push_str("null"),
        Value::Bool(b) => s.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => s.push_str(&n.to_string()),
        Value::String(t) => write_str(s, t),
        Value::Array(a) => {
            if a.is_empty() {
                s.push_str("[]");
                return;
            }
            s.push('[');
            for (i, x) in a.iter().enumerate() {
                if i > 0 {
                    s.push(',');
                }
                indent(s, level + 1);
                write_value(s, x, level + 1);
            }
            indent(s, level);
            s.push(']');
        }
        Value::Object(m) => {
            if m.is_empty() {
                s.push_str("{}");
                return;
            }
            // sorted here, whatever order the map keeps: str order is code point order for UTF-8
            let mut keys: Vec<&String> = m.keys().collect();
            keys.sort();
            s.push('{');
            for (i, k) in keys.into_iter().enumerate() {
                if i > 0 {
                    s.push(',');
                }
                indent(s, level + 1);
                write_str(s, k);
                s.push_str(": ");
                write_value(s, &m[k], level + 1);
            }
            indent(s, level);
            s.push('}');
        }
    }
}

fn write_str(s: &mut String, t: &str) {
    s.push('"');
    for c in t.chars() {
        match c {
            '"' => s.push_str("\\\""),
            '\\' => s.push_str("\\\\"),
            '\n' => s.push_str("\\n"),
            '\r' => s.push_str("\\r"),
            '\t' => s.push_str("\\t"),
            '\u{8}' => s.push_str("\\b"),
            '\u{c}' => s.push_str("\\f"),
            c if (c as u32) < 0x20 || (c as u32) > 0x7f => {
                let mut buf = [0u16; 2];
                for u in c.encode_utf16(&mut buf) {
                    let _ = write!(s, "\\u{u:04x}");
                }
            }
            c => s.push(c),
        }
    }
    s.push('"');
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn layout_matches_python() {
        let v = json!({"b": [1, 2], "a": {"z": null, "y": []}, "c": {}, "s": "x\"\\\n\t\u{1}é😀\u{7f}"});
        // python3 -c 'import json;print(json.dumps(..., sort_keys=True, indent=1, ensure_ascii=True))'
        let want = "{\n \"a\": {\n  \"y\": [],\n  \"z\": null\n },\n \"b\": [\n  1,\n  2\n ],\n \"c\": {},\n \"s\": \"x\\\"\\\\\\n\\t\\u0001\\u00e9\\ud83d\\ude00\u{7f}\"\n}\n";
        assert_eq!(to_string(&v), want);
    }
}
