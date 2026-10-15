//! Expressions (primitive P3): wherever a manifest takes a number or a symbol (write values, section lengths,
//! registry `first`, row words, hook targets).
//!
//! Grammar, C precedence, all values 64-bit signed with overflow checks:
//!
//! ```text
//! expr    = integer | symbol | "$" field | "image." ("u8"|"u16"|"u32") "(" anchor [("+"|"-") expr] ")"
//!         | "~" expr | "-" expr | expr op expr | "(" expr ")"       op = * / % + - << >> & ^ |
//! ```
//!
//! A symbol is `[A-Za-z_.][A-Za-z0-9_.]*` (anchor names, module exports, registry and hook symbols; `this` is the
//! anchor of the current multi-site op). `$name` is a field of the registry entry a section row is built for.
//! `image.uN(anchor + off)` is the N-bit value stored in the stock image at that place of the anchor (big endian
//! on ColdFire, little endian in the SHARC stream); the read must lie inside the anchor, so it is covered by the
//! anchor's sha256.

use crate::error::*;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Expr {
    Num(i64),
    Sym(String),
    Field(String),
    Neg(Box<Expr>),
    Not(Box<Expr>),
    Bin(Op, Box<Expr>, Box<Expr>),
    Image {
        width: u8,
        anchor: String,
        off: Option<Box<Expr>>,
    },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Op {
    Mul,
    Div,
    Rem,
    Add,
    Sub,
    Shl,
    Shr,
    And,
    Xor,
    Or,
}

/// What an expression is evaluated against.
pub trait Env {
    fn symbol(&self, name: &str) -> Option<i64>;
    fn field(&self, name: &str) -> Option<i64>;
    /// `width` bytes of the stock image at `off` bytes into `anchor`.
    fn image(&self, anchor: &str, off: i64, width: u8) -> Result<i64>;
}

#[derive(Clone, Debug, PartialEq)]
enum Tok {
    Num(i64),
    Id(String),
    Field(String),
    Sym(&'static str),
}

fn lex(s: &str) -> Result<Vec<Tok>> {
    let b: Vec<char> = s.chars().collect();
    let mut i = 0;
    let mut out = vec![];
    while i < b.len() {
        let c = b[i];
        if c.is_whitespace() {
            i += 1;
        } else if c.is_ascii_digit() {
            let st = i;
            while i < b.len() && (b[i].is_ascii_alphanumeric() || b[i] == '_') {
                i += 1;
            }
            let t: String = b[st..i].iter().filter(|c| **c != '_').collect();
            let v = if let Some(h) = t.strip_prefix("0x").or_else(|| t.strip_prefix("0X")) {
                i64::from_str_radix(h, 16)
            } else if let Some(h) = t.strip_prefix("0b") {
                i64::from_str_radix(h, 2)
            } else {
                t.parse::<i64>()
            }
            .map_err(|_| err!("expression {s:?}: bad number {t:?}"))?;
            out.push(Tok::Num(v));
        } else if c.is_ascii_alphabetic() || c == '_' || c == '.' {
            let st = i;
            while i < b.len() && (b[i].is_ascii_alphanumeric() || b[i] == '_' || b[i] == '.') {
                i += 1;
            }
            out.push(Tok::Id(b[st..i].iter().collect()));
        } else if c == '$' {
            i += 1;
            let st = i;
            while i < b.len() && (b[i].is_ascii_alphanumeric() || b[i] == '_') {
                i += 1;
            }
            if st == i {
                return Err(err!("expression {s:?}: `$` needs a field name"));
            }
            out.push(Tok::Field(b[st..i].iter().collect()));
        } else {
            let two: String = b[i..(i + 2).min(b.len())].iter().collect();
            let t = match two.as_str() {
                "<<" => Some("<<"),
                ">>" => Some(">>"),
                _ => None,
            };
            if let Some(t) = t {
                out.push(Tok::Sym(t));
                i += 2;
                continue;
            }
            let t = match c {
                '+' => "+",
                '-' => "-",
                '*' => "*",
                '/' => "/",
                '%' => "%",
                '&' => "&",
                '|' => "|",
                '^' => "^",
                '~' => "~",
                '(' => "(",
                ')' => ")",
                _ => return Err(err!("expression {s:?}: unexpected {c:?}")),
            };
            out.push(Tok::Sym(t));
            i += 1;
        }
    }
    Ok(out)
}

struct P<'a> {
    src: &'a str,
    t: Vec<Tok>,
    at: usize,
}

impl P<'_> {
    fn peek(&self) -> Option<&Tok> {
        self.t.get(self.at)
    }
    fn eat(&mut self, s: &'static str) -> bool {
        if self.peek() == Some(&Tok::Sym(s)) {
            self.at += 1;
            true
        } else {
            false
        }
    }
    fn fail<T>(&self, what: &str) -> Result<T> {
        Err(err!("expression {:?}: {what}", self.src))
    }
    /// One precedence level: `ops` from tightest-binding-first table.
    fn level(&mut self, lvl: usize) -> Result<Expr> {
        const LEVELS: [&[(&str, Op)]; 6] = [
            &[("*", Op::Mul), ("/", Op::Div), ("%", Op::Rem)],
            &[("+", Op::Add), ("-", Op::Sub)],
            &[("<<", Op::Shl), (">>", Op::Shr)],
            &[("&", Op::And)],
            &[("^", Op::Xor)],
            &[("|", Op::Or)],
        ];
        let mut lhs = if lvl == 0 {
            self.unary()?
        } else {
            self.level(lvl - 1)?
        };
        'outer: loop {
            for (s, op) in LEVELS[lvl] {
                if self.eat(s) {
                    let rhs = if lvl == 0 {
                        self.unary()?
                    } else {
                        self.level(lvl - 1)?
                    };
                    lhs = Expr::Bin(*op, Box::new(lhs), Box::new(rhs));
                    continue 'outer;
                }
            }
            return Ok(lhs);
        }
    }
    fn unary(&mut self) -> Result<Expr> {
        if self.eat("-") {
            return Ok(Expr::Neg(Box::new(self.unary()?)));
        }
        if self.eat("~") {
            return Ok(Expr::Not(Box::new(self.unary()?)));
        }
        if self.eat("(") {
            let e = self.level(5)?;
            if !self.eat(")") {
                return self.fail("missing `)`");
            }
            return Ok(e);
        }
        match self.t.get(self.at).cloned() {
            Some(Tok::Num(n)) => {
                self.at += 1;
                Ok(Expr::Num(n))
            }
            Some(Tok::Field(f)) => {
                self.at += 1;
                Ok(Expr::Field(f))
            }
            Some(Tok::Id(id)) => {
                self.at += 1;
                if self.peek() == Some(&Tok::Sym("(")) {
                    let width = match id.as_str() {
                        "image.u8" => 1,
                        "image.u16" => 2,
                        "image.u32" => 4,
                        _ => return self.fail(&format!("unknown function {id}")),
                    };
                    self.at += 1;
                    let Some(Tok::Id(anchor)) = self.t.get(self.at).cloned() else {
                        return self.fail("image.uN( needs an anchor name");
                    };
                    self.at += 1;
                    let off = if self.eat("+") {
                        Some(Box::new(self.level(5)?))
                    } else if self.peek() == Some(&Tok::Sym("-")) {
                        // `anchor - 4`: the sign belongs to the offset expression
                        Some(Box::new(self.level(5)?))
                    } else {
                        None
                    };
                    if !self.eat(")") {
                        return self.fail("missing `)` after image.uN(..");
                    }
                    Ok(Expr::Image { width, anchor, off })
                } else {
                    Ok(Expr::Sym(id))
                }
            }
            _ => self.fail("expected a number, a symbol or `(`"),
        }
    }
}

pub fn parse(src: &str) -> Result<Expr> {
    let mut p = P {
        src,
        t: lex(src)?,
        at: 0,
    };
    if p.t.is_empty() {
        return Err(err!("empty expression"));
    }
    let e = p.level(5)?;
    if p.at != p.t.len() {
        return p.fail("unexpected text after the expression");
    }
    Ok(e)
}

impl Expr {
    /// Every symbol and anchor name the expression mentions (to find the anchors a module uses).
    pub fn names(&self, out: &mut Vec<String>) {
        match self {
            Expr::Num(_) | Expr::Field(_) => {}
            Expr::Sym(s) => out.push(s.clone()),
            Expr::Neg(e) | Expr::Not(e) => e.names(out),
            Expr::Bin(_, a, b) => {
                a.names(out);
                b.names(out);
            }
            Expr::Image { anchor, off, .. } => {
                out.push(anchor.clone());
                if let Some(o) = off {
                    o.names(out);
                }
            }
        }
    }

    pub fn eval(&self, env: &dyn Env) -> Result<i64> {
        let ov = || err!("expression overflows 64 bits");
        Ok(match self {
            Expr::Num(n) => *n,
            Expr::Sym(s) => env.symbol(s).ok_or_else(|| err!("unknown symbol {s}"))?,
            Expr::Field(f) => env
                .field(f)
                .ok_or_else(|| err!("unknown field ${f} (only a section row has fields)"))?,
            Expr::Neg(e) => e.eval(env)?.checked_neg().ok_or_else(ov)?,
            Expr::Not(e) => !e.eval(env)?,
            Expr::Image { width, anchor, off } => {
                let o = match off {
                    Some(e) => e.eval(env)?,
                    None => 0,
                };
                env.image(anchor, o, *width)?
            }
            Expr::Bin(op, a, b) => {
                let (x, y) = (a.eval(env)?, b.eval(env)?);
                match op {
                    Op::Mul => x.checked_mul(y).ok_or_else(ov)?,
                    Op::Add => x.checked_add(y).ok_or_else(ov)?,
                    Op::Sub => x.checked_sub(y).ok_or_else(ov)?,
                    Op::Div | Op::Rem if y == 0 => return Err(err!("division by zero")),
                    Op::Div => x.checked_div(y).ok_or_else(ov)?,
                    Op::Rem => x.checked_rem(y).ok_or_else(ov)?,
                    Op::Shl | Op::Shr if !(0..63).contains(&y) => {
                        return Err(err!("shift by {y}"));
                    }
                    Op::Shl => x.checked_mul(1i64 << y).ok_or_else(ov)?,
                    Op::Shr => x >> y,
                    Op::And => x & y,
                    Op::Xor => x ^ y,
                    Op::Or => x | y,
                }
            }
        })
    }
}

/// Parse and evaluate in one step.
pub fn eval_str(src: &str, env: &dyn Env) -> Result<i64> {
    parse(src)?.eval(env)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;

    struct E(BTreeMap<&'static str, i64>);
    impl Env for E {
        fn symbol(&self, n: &str) -> Option<i64> {
            self.0.get(n).copied()
        }
        fn field(&self, n: &str) -> Option<i64> {
            self.0.get(format!("${n}").as_str()).copied()
        }
        fn image(&self, a: &str, off: i64, w: u8) -> Result<i64> {
            if a != "stock" {
                return Err(err!("no anchor {a}"));
            }
            Ok(0x1000 + off * 16 + w as i64)
        }
    }
    fn env() -> E {
        E(BTreeMap::from([
            ("a", 10),
            ("machines.first", 7),
            ("$idx", 3),
        ]))
    }
    fn ev(s: &str) -> Result<i64> {
        eval_str(s, &env())
    }

    #[test]
    fn precedence_and_operators() {
        assert_eq!(ev("1 + 2 * 3").unwrap(), 7);
        assert_eq!(ev("(1 + 2) * 3").unwrap(), 9);
        assert_eq!(ev("1 << 4 | 3").unwrap(), 19);
        assert_eq!(ev("0xff & ~0x0f").unwrap(), 0xf0);
        assert_eq!(ev("-a + 20 % 6 - 8 / 2").unwrap(), -12);
        assert_eq!(ev("6 ^ 3").unwrap(), 5);
        assert_eq!(ev("0x100 >> 4 + 0").unwrap(), 16);
        assert_eq!(ev("1 + 2 << 1").unwrap(), 6); // + binds tighter than <<
        assert_eq!(ev("machines.first + $idx").unwrap(), 10);
    }

    #[test]
    fn image_reads_take_an_anchor_and_an_offset() {
        assert_eq!(ev("image.u32(stock)").unwrap(), 0x1004);
        assert_eq!(ev("image.u16(stock + 2)").unwrap(), 0x1022);
        assert_eq!(ev("image.u8(stock - 1 + 1)").unwrap(), 0x1001);
        assert_eq!(ev("image.u32(stock + 1) + 1").unwrap(), 0x1015);
        let mut n = vec![];
        parse("image.u32(stock + a) + b").unwrap().names(&mut n);
        assert_eq!(n, ["stock", "a", "b"]);
    }

    #[test]
    fn errors_name_the_problem() {
        assert!(ev("1 +").unwrap_err().0.contains("expected a number"));
        assert!(ev("nope").unwrap_err().0.contains("unknown symbol nope"));
        assert!(ev("$zz").unwrap_err().0.contains("unknown field"));
        assert!(ev("1 / 0").unwrap_err().0.contains("division by zero"));
        assert!(ev("1 << 64").unwrap_err().0.contains("shift"));
        assert!(ev("(1").unwrap_err().0.contains("missing"));
        assert!(ev("1 2").unwrap_err().0.contains("unexpected text"));
        assert!(
            ev("image.u64(stock)")
                .unwrap_err()
                .0
                .contains("unknown function")
        );
        assert!(
            ev("0x7fffffffffffffff + 1")
                .unwrap_err()
                .0
                .contains("overflow")
        );
        assert!(ev("1 # 2").unwrap_err().0.contains("unexpected"));
    }
}
