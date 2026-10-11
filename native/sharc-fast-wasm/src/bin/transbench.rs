//! `transbench OUT.wasm`: a module for measuring what one kernel-to-kernel
//! transition costs in WebAssembly (`transbench.mjs` runs it):
//!  * `k0(ctx, st) -> i32`: an empty kernel;
//!  * `direct(n)`: n direct calls of `k0` from inside the module;
//!  * `indirect(n)`: n `call_indirect` calls through a table (a dispatch on
//!    a computed target, as chaining between regions needs);
//!  * `dispatch(n)`: n transitions through a `br_table` on a counter (the
//!    in-function dispatch of a single-function whole-image module).

use wasm_encoder::{
    BlockType, CodeSection, ConstExpr, ElementSection, Elements, ExportKind, ExportSection,
    Function, FunctionSection, Instruction as I, Module, RefType, TableSection, TableType,
    TypeSection, ValType,
};

fn main() {
    let out = std::env::args().nth(1).expect("usage: transbench OUT.wasm");
    let mut m = Module::new();
    let mut types = TypeSection::new();
    types
        .ty()
        .function([ValType::I32, ValType::I32], [ValType::I32]);
    types.ty().function([ValType::I32], [ValType::I32]);
    m.section(&types);
    let mut funcs = FunctionSection::new();
    for t in [0, 1, 1, 1] {
        funcs.function(t);
    }
    m.section(&funcs);
    let mut tables = TableSection::new();
    tables.table(TableType {
        element_type: RefType::FUNCREF,
        minimum: 1,
        maximum: None,
        table64: false,
        shared: false,
    });
    m.section(&tables);
    let mut exports = ExportSection::new();
    for (i, n) in ["k0", "direct", "indirect", "dispatch"].iter().enumerate() {
        exports.export(n, ExportKind::Func, i as u32);
    }
    m.section(&exports);
    let mut elems = ElementSection::new();
    elems.active(
        Some(0),
        &ConstExpr::i32_const(0),
        Elements::Functions(vec![0].into()),
    );
    m.section(&elems);
    let mut code = CodeSection::new();
    // k0: return 0
    let mut f = Function::new([]);
    f.instruction(&I::I32Const(0)).instruction(&I::End);
    code.function(&f);
    // loops over local 1 = i, local 2 = acc
    let looped = |body: &dyn Fn(&mut Function)| {
        let mut f = Function::new([(2, ValType::I32)]);
        f.instruction(&I::Block(BlockType::Empty));
        f.instruction(&I::Loop(BlockType::Empty));
        f.instruction(&I::LocalGet(1))
            .instruction(&I::LocalGet(0))
            .instruction(&I::I32GeU)
            .instruction(&I::BrIf(1));
        body(&mut f);
        f.instruction(&I::LocalGet(1))
            .instruction(&I::I32Const(1))
            .instruction(&I::I32Add)
            .instruction(&I::LocalSet(1));
        f.instruction(&I::Br(0));
        f.instruction(&I::End).instruction(&I::End);
        f.instruction(&I::LocalGet(2)).instruction(&I::End);
        f
    };
    code.function(&looped(&|f| {
        f.instruction(&I::I32Const(0))
            .instruction(&I::I32Const(0))
            .instruction(&I::Call(0))
            .instruction(&I::LocalGet(2))
            .instruction(&I::I32Add)
            .instruction(&I::LocalSet(2));
    }));
    code.function(&looped(&|f| {
        f.instruction(&I::I32Const(0))
            .instruction(&I::I32Const(0))
            .instruction(&I::I32Const(0))
            .instruction(&I::CallIndirect {
                type_index: 0,
                table_index: 0,
            })
            .instruction(&I::LocalGet(2))
            .instruction(&I::I32Add)
            .instruction(&I::LocalSet(2));
    }));
    // dispatch: a 4-way br_table on i & 3, each arm adds to acc
    code.function(&looped(&|f| {
        for _ in 0..4 {
            f.instruction(&I::Block(BlockType::Empty));
        }
        f.instruction(&I::LocalGet(1))
            .instruction(&I::I32Const(3))
            .instruction(&I::I32And)
            .instruction(&I::BrTable(vec![0, 1, 2, 3].into(), 3));
        for k in 0..4 {
            f.instruction(&I::End);
            f.instruction(&I::LocalGet(2))
                .instruction(&I::I32Const(k + 1))
                .instruction(&I::I32Add)
                .instruction(&I::LocalSet(2));
        }
    }));
    m.section(&code);
    std::fs::write(&out, m.finish()).unwrap();
}
