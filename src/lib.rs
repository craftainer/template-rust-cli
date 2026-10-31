//! Example command-line program: a tiny, fully tested CLI to replace.
//!
//! The binary (`src/main.rs`) only forwards its arguments and streams to
//! [`run`], so the whole program is testable without spawning a process;
//! `tests/cli.rs` still spawns the real binary once, as a user would.

use std::ffi::OsString;
use std::fmt;
use std::io::Write;
use std::process::ExitCode;

/// Why [`greet`] refused its input.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GreetError {
    /// The name was empty or only whitespace.
    EmptyName,
}

impl fmt::Display for GreetError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::EmptyName => f.write_str("name must not be empty"),
        }
    }
}

impl std::error::Error for GreetError {}

/// Builds a greeting for `name`.
///
/// # Errors
///
/// Returns [`GreetError::EmptyName`] when `name` is empty or only whitespace.
///
/// # Examples
///
/// ```
/// assert_eq!(template_rust_cli::greet("Ada").unwrap(), "Hello, Ada!");
/// ```
pub fn greet(name: &str) -> Result<String, GreetError> {
    let name = name.trim();
    if name.is_empty() {
        return Err(GreetError::EmptyName);
    }
    Ok(format!("Hello, {name}!"))
}

/// The text `--help` prints.
const USAGE: &str = "Usage: template-rust-cli [OPTIONS] <NAME>\n\
\n\
Prints a greeting for NAME.\n\
\n\
Options:\n  -h, --help     Print this help\n  -V, --version  Print the version\n";

/// Runs the program with `args` (without the program name), writing to `out`
/// and `err`, and returns the process exit code: 0 on success, 1 when the
/// greeting is refused, 2 for a usage error.
pub fn run<I>(args: I, out: &mut impl Write, err: &mut impl Write) -> ExitCode
where
    I: IntoIterator<Item = OsString>,
{
    let args: Vec<String> = args
        .into_iter()
        .map(|arg| arg.to_string_lossy().into_owned())
        .collect();
    let written = match args.as_slice() {
        [flag] if flag == "-h" || flag == "--help" => write!(out, "{USAGE}"),
        [flag] if flag == "-V" || flag == "--version" => {
            writeln!(out, "template-rust-cli {}", env!("CARGO_PKG_VERSION"))
        }
        [name] if !name.starts_with('-') => match greet(name) {
            Ok(greeting) => writeln!(out, "{greeting}"),
            Err(error) => {
                return finish(&writeln!(err, "error: {error}"), 1);
            }
        },
        _ => {
            return finish(&write!(err, "{USAGE}"), 2);
        }
    };
    finish(&written, 0)
}

/// Maps a write result to `code`, or to 74 (`EX_IOERR`) when the stream broke.
fn finish(written: &std::io::Result<()>, code: u8) -> ExitCode {
    match written {
        Ok(()) => ExitCode::from(code),
        Err(_) => ExitCode::from(74),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn exec(args: &[&str]) -> (ExitCode, String, String) {
        let (mut out, mut err) = (Vec::new(), Vec::new());
        let code = run(args.iter().map(OsString::from), &mut out, &mut err);
        (
            code,
            String::from_utf8(out).unwrap(),
            String::from_utf8(err).unwrap(),
        )
    }

    /// A stream that refuses every write.
    struct Broken;

    impl Write for Broken {
        fn write(&mut self, _: &[u8]) -> std::io::Result<usize> {
            Err(std::io::ErrorKind::BrokenPipe.into())
        }

        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }

    #[test]
    fn greets_a_name() {
        assert_eq!(greet("  Ada ").unwrap(), "Hello, Ada!");
    }

    #[test]
    fn rejects_blank_names() {
        assert_eq!(greet(" ").unwrap_err(), GreetError::EmptyName);
    }

    #[test]
    fn error_displays_a_message() {
        assert_eq!(GreetError::EmptyName.to_string(), "name must not be empty");
    }

    #[test]
    fn prints_a_greeting() {
        let (code, out, err) = exec(&["Ada"]);
        assert_eq!(code, ExitCode::SUCCESS);
        assert_eq!(out, "Hello, Ada!\n");
        assert_eq!(err, "");
    }

    #[test]
    fn prints_help_and_version() {
        for flag in ["-h", "--help"] {
            let (code, out, _) = exec(&[flag]);
            assert_eq!(code, ExitCode::SUCCESS);
            assert_eq!(out, USAGE);
        }
        for flag in ["-V", "--version"] {
            let (code, out, _) = exec(&[flag]);
            assert_eq!(code, ExitCode::SUCCESS);
            assert_eq!(
                out,
                format!("template-rust-cli {}\n", env!("CARGO_PKG_VERSION"))
            );
        }
    }

    #[test]
    fn refuses_a_blank_name() {
        let (code, out, err) = exec(&[" "]);
        assert_eq!(code, ExitCode::from(1));
        assert_eq!(out, "");
        assert_eq!(err, "error: name must not be empty\n");
    }

    #[test]
    fn rejects_bad_usage() {
        for args in [&[][..], &["--nope"], &["a", "b"]] {
            let (code, out, err) = exec(args);
            assert_eq!(code, ExitCode::from(2));
            assert_eq!(out, "");
            assert_eq!(err, USAGE);
        }
    }

    #[test]
    fn broken_streams_exit_74() {
        assert!(Broken.flush().is_ok());
        let ok = |args: &[&str], out: &mut Broken, err: &mut Broken| {
            run(args.iter().map(OsString::from), out, err)
        };
        for args in [&["Ada"][..], &["--help"], &[" "], &[]] {
            assert_eq!(ok(args, &mut Broken, &mut Broken), ExitCode::from(74));
        }
    }
}
