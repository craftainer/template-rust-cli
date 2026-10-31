//! Entry point: forwards the process arguments and standard streams to
//! [`template_rust_cli::run`], which holds all the logic.

use std::process::ExitCode;

fn main() -> ExitCode {
    template_rust_cli::run(
        std::env::args_os().skip(1),
        &mut std::io::stdout().lock(),
        &mut std::io::stderr().lock(),
    )
}
