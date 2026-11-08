//! Click-compatible shell completion for the ogcli Rust CLIs.
//!
//! Implements the same `_EDR_COMPLETE`/`_WMS_COMPLETE` env-var protocol
//! as the Python (click) CLIs, so the shell hooks in the user's rc file
//! work no matter which implementation is on PATH:
//! - `{VAR}={shell}_source <prog>`  -> prints the completion script
//! - `{VAR}={shell}_complete` with COMP_WORDS/COMP_CWORD -> prints
//!   completions (zsh: "type\nvalue\nhelp" triplets, bash/fish:
//!   "type,value" pairs)
//!
//! Completions for query arguments are fuzzy (same scoring as the
//! resolvers), so 'temp' completes 'gfs_TMP' etc.

use ogcli_core::edr::config as ec;
use ogcli_core::wms::config as wc;

const ZSH_SOURCE: &str = r#"#compdef {prog}

_{prog}_completion() {{
    local -a completions
    local -a completions_with_descriptions
    local -a response
    (( ! $+commands[{prog}] )) && return 1

    response=("${{(@f)$(env COMP_WORDS="${{words[*]}}" COMP_CWORD=$((CURRENT-1)) {var}={shell}_complete {prog})}}")

    for type key descr in ${{response}}; do
        if [[ "$type" == "plain" ]]; then
            if [[ "$descr" == "_" ]]; then
                completions+=("$key")
            else
                completions_with_descriptions+=("$key":"$descr")
            fi
        elif [[ "$type" == "dir" ]]; then
            _path_files -/
        elif [[ "$type" == "file" ]]; then
            _path_files -f
        fi
    done

    if [ -n "$completions_with_descriptions" ]; then
        _describe -V unsorted completions_with_descriptions -U
    fi

    if [ -n "$completions" ]; then
        compadd -U -V unsorted -a completions
    fi
}}

if [[ $zsh_eval_context[-1] == loadautofunc ]]; then
    # autoload from fpath, call function directly
    _{prog}_completion "$@"
else
    # eval/source/. command, register function for later
    compdef _{prog}_completion {prog}
fi
"#;

const BASH_SOURCE: &str = r#"_completion() {
    local IFS=$'\n'
    local response

    response=$(env COMP_WORDS="${COMP_WORDS[*]}" COMP_CWORD=$COMP_CWORD {var}={shell}_complete $1)

    for completion in $response; do
        IFS=',' read type value <<< "$completion"

        if [[ $type == 'dir' ]]; then
            COMPREPLY=()
            compopt -o dirnames
        elif [[ $type == 'file' ]]; then
            COMPREPLY=()
            compopt -o default
        elif [[ $type == 'plain' ]]; then
            COMPREPLY+=("$value")
        fi
    done
}

complete -o nosort -F _completion {prog}
"#;

const FISH_SOURCE: &str = r#"function _completion;
    set -l response (env {var}={shell}_complete COMP_WORDS=(commandline -cp) COMP_CWORD=(commandline -t) {prog});

    for completion in $response;
        set -l metadata (string split "," $completion);

        if test $metadata[1] = "dir";
            __fish_complete_directories $metadata[2];
        else if test $metadata[1] = "file";
            __fish_complete_path $metadata[2];
        else if test $metadata[1] = "plain";
            echo $metadata[2];
        end;
    end;
end;

complete --command {prog} --no-files --arguments "(_completion)";
"#;

/// A single completion candidate.
struct Cand {
    value: String,
    help: String,
}

/// Which CLI is completing (drives subcommands + fuzzy data source).
#[derive(Clone, Copy, PartialEq)]
pub enum Tool {
    Edr,
    Wms,
}

impl Tool {
    fn complete_var(self) -> &'static str {
        match self {
            Tool::Edr => "_EDR_COMPLETE",
            Tool::Wms => "_WMS_COMPLETE",
        }
    }

    fn subcommands(self) -> &'static [&'static str] {
        match self {
            Tool::Edr => &[
                "init", "status", "profiles", "collections", "describe",
                "position", "radius", "area", "cube", "trajectory", "corridor",
                "locations", "instances", "url", "hammer", "cache", "set",
            ],
            Tool::Wms => &[
                "init", "status", "profiles", "layers", "describe",
                "map", "tile", "legend", "url", "cache", "set",
            ],
        }
    }

    /// Commands whose first positional args are fuzzy queries.
    fn query_commands(self) -> &'static [&'static str] {
        match self {
            Tool::Edr => &[
                "collections", "describe", "position", "radius", "area", "cube",
                "trajectory", "corridor", "locations", "instances", "url", "hammer",
            ],
            Tool::Wms => &["layers", "describe", "map", "tile", "legend", "url"],
        }
    }
}

/// Entry point: handle the completion env var, returning Some(exit code)
/// if this invocation was a completion request (and normal parsing
/// should be skipped).
pub fn handle(tool: Tool, prog: &str) -> Option<i32> {
    let instruction = std::env::var(tool.complete_var()).ok()?;
    let (shell, action) = instruction.split_once('_')?;

    let out = match action {
        "source" => {
            let template = match shell {
                "zsh" => ZSH_SOURCE,
                "bash" => BASH_SOURCE,
                "fish" => FISH_SOURCE,
                _ => return None,
            };
            template
                .replace("{prog}", prog)
                .replace("{var}", tool.complete_var())
                .replace("{shell}", shell)
                // Unescape the {{ / }} brace escapes used inside the
                // raw-string templates (Python .format style)
                .replace("{{", "\x7b")
                .replace("}}", "\x7d")
                .replace("\x7b", "{")
                .replace("\x7d", "}")
        }
        "complete" => {
            let words = std::env::var("COMP_WORDS").unwrap_or_default();
            let cword: usize = std::env::var("COMP_CWORD")
                .ok()
                .and_then(|v| v.trim().parse().ok())
                .unwrap_or(0);
            let cwords = split_words(&words);
            let args: Vec<String> = cwords.iter().take(cword).skip(1).cloned().collect();
            let incomplete = cwords.get(cword).cloned().unwrap_or_default();
            let cands = complete(tool, &args, &incomplete);
            format_candidates(&cands, shell)
        }
        _ => return None,
    };
    print!("{out}");
    Some(0)
}

/// Split COMP_WORDS honoring quotes (click uses shlex).
fn split_words(s: &str) -> Vec<String> {
    let mut words = Vec::new();
    let mut cur = String::new();
    let mut quote: Option<char> = None;
    for c in s.chars() {
        match quote {
            Some(q) if c == q => quote = None,
            Some(_) => cur.push(c),
            None => match c {
                '\'' | '"' => quote = Some(c),
                c if c.is_whitespace() => {
                    if !cur.is_empty() {
                        words.push(std::mem::take(&mut cur));
                    }
                }
                c => cur.push(c),
            },
        }
    }
    if !cur.is_empty() {
        words.push(cur);
    }
    words
}

/// Decide what's being completed and produce candidates.
fn complete(tool: Tool, args: &[String], incomplete: &str) -> Vec<Cand> {
    // Classify args: track global flags and their values, keep
    // positional words.
    let mut positional: Vec<String> = Vec::new();
    let mut completing_profile_value = false;
    let mut skip_next = false;
    for (i, a) in args.iter().enumerate() {
        if skip_next {
            skip_next = false;
            continue;
        }
        if a == "-p" || a == "--profile" {
            match args.get(i + 1) {
                None => completing_profile_value = true,
                Some(_) => skip_next = true,
            }
            continue;
        }
        if a.starts_with("--profile=") || a.starts_with('-') {
            continue;
        }
        positional.push(a.clone());
    }

    if completing_profile_value {
        return profile_candidates(tool, incomplete);
    }

    match positional.first() {
        None => {
            let mut cmds: Vec<&str> = tool
                .subcommands()
                .iter()
                .filter(|c| c.starts_with(incomplete))
                .copied()
                .collect();
            cmds.sort();
            cmds.into_iter()
                .map(|c| Cand {
                    value: c.to_string(),
                    help: String::new(),
                })
                .collect()
        }
        Some(cmd) => {
            if tool.query_commands().contains(&cmd.as_str()) {
                fuzzy_candidates(tool, incomplete)
            } else if cmd == "set" {
                // 'set profile <TAB>' completes profile names
                if positional.get(1).map(|s| s.as_str()) == Some("profile") {
                    profile_candidates(tool, incomplete)
                } else if incomplete.starts_with("p") {
                    vec![Cand { value: "profile".into(), help: String::new() }]
                } else {
                    vec![]
                }
            } else if cmd == "cache" {
                ["status", "clear", "refresh"]
                    .iter()
                    .filter(|c| c.starts_with(incomplete))
                    .map(|c| Cand {
                        value: c.to_string(),
                        help: String::new(),
                    })
                    .collect()
            } else {
                vec![]
            }
        }
    }
}

fn profile_candidates(tool: Tool, incomplete: &str) -> Vec<Cand> {
    let (names, active) = match tool {
        Tool::Edr => {
            let c = ec::load_config();
            let a = c.active.clone();
            (c.profile_names(), a)
        }
        Tool::Wms => {
            let c = wc::load_config();
            let a = c.active.clone();
            (c.profile_names(), a)
        }
    };
    let mut names = names;
    names.sort();
    names
        .into_iter()
        .filter(|n| n.starts_with(incomplete))
        .map(|n| Cand {
            help: if n == active {
                "active".into()
            } else {
                String::new()
            },
            value: n,
        })
        .collect()
}

/// Fuzzy collection/layer candidates from the active profile's cache.
fn fuzzy_candidates(tool: Tool, incomplete: &str) -> Vec<Cand> {
    if incomplete.is_empty() {
        return names(tool, 50)
            .into_iter()
            .map(|(v, h)| Cand { value: v, help: h })
            .collect();
    }
    let lower = incomplete.to_lowercase();
    let all = names(tool, 2000);

    // Prefix/substring matches first, then fuzzy-scored matches
    let mut prefix: Vec<(String, String)> = Vec::new();
    let mut substr: Vec<(String, String)> = Vec::new();
    let mut fuzzy: Vec<(f64, String, String)> = Vec::new();
    for (name, help) in all {
        let n = name.to_lowercase();
        if n.starts_with(&lower) {
            prefix.push((name, help));
        } else if n.contains(&lower) {
            substr.push((name, help));
        } else {
            let score = ogcli_core::fuzz::partial_ratio(&lower, &n);
            if score > 60.0 {
                fuzzy.push((score, name, help));
            }
        }
    }
    fuzzy.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));

    let mut out: Vec<Cand> = Vec::new();
    for (v, h) in prefix.into_iter().chain(substr) {
        out.push(Cand { value: v, help: h });
    }
    for (_, v, h) in fuzzy {
        out.push(Cand { value: v, help: h });
    }
    out.truncate(50);
    out
}

/// All names (+ help) for the tool's active profile, from cache only
/// (never hits the network - completions must be instant).
fn names(tool: Tool, limit: usize) -> Vec<(String, String)> {
    match tool {
        Tool::Edr => {
            let config = ec::load_config();
            let Ok(json) = ec::get_collections_json(&config) else {
                return vec![];
            };
            let Ok(caps) = ogcli_core::edr::parser::parse_collections(&json, "") else {
                return vec![];
            };
            caps.collections
                .into_iter()
                .take(limit)
                .map(|c| (c.id, c.title))
                .collect()
        }
        Tool::Wms => {
            let config = wc::load_config();
            let Ok(xml) = wc::get_capabilities_xml(&config) else {
                return vec![];
            };
            let Ok(caps) = ogcli_core::wms::parser::parse_capabilities(&xml) else {
                return vec![];
            };
            caps.queryable_layers()
                .into_iter()
                .take(limit)
                .map(|l| (l.name.clone().unwrap_or_default(), l.title.clone()))
                .collect()
        }
    }
}

fn format_candidates(cands: &[Cand], shell: &str) -> String {
    let mut out = String::new();
    for c in cands {
        match shell {
            // zsh: type\nvalue\nhelp triplets (_ for no help)
            "zsh" => {
                let help = if c.help.is_empty() { "_" } else { &c.help };
                out.push_str(&format!("plain\n{}\n{}\n", c.value, help));
            }
            // bash/fish: type,value pairs
            _ => out.push_str(&format!("plain,{}\n", c.value)),
        }
    }
    out
}
