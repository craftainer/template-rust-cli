//! Integration tier: spawns the compiled binary, as a user's shell would.

use std::process::{Command, Output};

fn cli(args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_template-rust-cli"))
        .args(args)
        .output()
        .expect("the binary runs")
}

#[test]
fn greets_on_stdout() {
    let output = cli(&["Grace"]);
    assert!(output.status.success());
    assert_eq!(String::from_utf8_lossy(&output.stdout), "Hello, Grace!\n");
}

#[test]
fn reports_bad_usage_with_exit_code_2() {
    let output = cli(&[]);
    assert_eq!(output.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&output.stderr).starts_with("Usage: "));
}

#[test]
fn the_library_api_is_public() {
    assert_eq!(template_rust_cli::greet("Grace").unwrap(), "Hello, Grace!");
    assert_eq!(
        template_rust_cli::greet("").unwrap_err(),
        template_rust_cli::GreetError::EmptyName
    );
}
