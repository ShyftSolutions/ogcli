//! String similarity primitives matching the Python implementation's
//! use of rapidfuzz (`ratio`, `partial_ratio`).
//!
//! The scores here feed the empirically-tuned resolver thresholds, so
//! the formulas must match rapidfuzz exactly:
//! - `ratio(a, b)` = 100 * (2 * LCS) / (len(a) + len(b))
//!   (rapidfuzz's generalized Levenshtein with substitution cost 2 is
//!   equivalent to the LCS/indel formulation)
//! - `partial_ratio(query, text)` = max ratio of the shorter string
//!   against every same-length window of the longer one.

/// Length of the longest common subsequence (case: chars).
fn lcs_len(a: &[char], b: &[char]) -> usize {
    if a.is_empty() || b.is_empty() {
        return 0;
    }
    // Rolling-row DP: O(len(a) * len(b)) time, O(len(b)) space.
    let mut prev = vec![0usize; b.len() + 1];
    let mut cur = vec![0usize; b.len() + 1];
    for &ca in a {
        for (j, &cb) in b.iter().enumerate() {
            cur[j + 1] = if ca == cb {
                prev[j] + 1
            } else {
                cur[j].max(prev[j + 1])
            };
        }
        std::mem::swap(&mut prev, &mut cur);
        cur.iter_mut().for_each(|v| *v = 0);
    }
    prev[b.len()]
}

/// rapidfuzz-compatible similarity ratio in 0..=100.
pub fn ratio(a: &str, b: &str) -> f64 {
    let a: Vec<char> = a.chars().collect();
    let b: Vec<char> = b.chars().collect();
    let total = a.len() + b.len();
    if total == 0 {
        return 100.0;
    }
    100.0 * (2.0 * lcs_len(&a, &b) as f64) / total as f64
}

/// rapidfuzz-compatible partial ratio in 0..=100: the best `ratio` of
/// the shorter string against any same-length window of the longer.
pub fn partial_ratio(a: &str, b: &str) -> f64 {
    let (short, long): (Vec<char>, Vec<char>) = {
        let x: Vec<char> = a.chars().collect();
        let y: Vec<char> = b.chars().collect();
        if x.len() <= y.len() { (x, y) } else { (y, x) }
    };
    if short.is_empty() || long.is_empty() {
        return if short.len() == long.len() { 100.0 } else { 0.0 };
    }
    let n = short.len();
    if long.len() == n {
        return ratio(&String::from_iter(short.iter()),
                     &String::from_iter(long.iter()));
    }
    let mut best = 0.0f64;
    for start in 0..=(long.len() - n) {
        let window: String = long[start..start + n].iter().collect();
        let short_s: String = short.iter().collect();
        let r = ratio(&short_s, &window);
        if r > best {
            best = r;
        }
        if best >= 100.0 {
            break;
        }
    }
    best
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ratio_basics() {
        assert!((ratio("abc", "abc") - 100.0).abs() < 1e-9);
        assert!((ratio("", "") - 100.0).abs() < 1e-9);
        // LCS=1 ("t"), lengths 1+3 -> 2/4 = 50
        assert!((ratio("t", "cat") - 50.0).abs() < 1e-9);
        // temp vs tmp: LCS = tmp (3), 2*3/7 = 85.71 (matches rapidfuzz,
        // which the Python resolver relies on for 'temp' -> TMP)
        let r = ratio("temp", "tmp");
        assert!(r > 85.0 && r < 86.0, "temp/tmp ratio was {r}");
    }

    #[test]
    fn partial_ratio_finds_substring() {
        // 'temp' appears inside 'temperature' -> 100
        assert!((partial_ratio("temp", "xx temperature yy") - 100.0).abs() < 1e-9);
        // No common chars -> low
        assert!(partial_ratio("xyz", "abc abc") < 50.0);
    }

    #[test]
    fn partial_ratio_longer_query_swaps() {
        assert!((partial_ratio("gfs temperature", "gfs") - 100.0).abs() < 1e-9);
    }
}
