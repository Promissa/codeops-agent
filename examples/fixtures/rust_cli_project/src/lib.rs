pub fn parse_token(value: &str) -> Result<&str, &'static str> {
    if value.is_empty() {
        Err("invalid token")
    } else {
        Ok(value)
    }
}

#[cfg(test)]
mod tests {
    use super::parse_token;

    #[test]
    fn rejects_invalid_token() {
        assert_eq!(parse_token(""), Err("invalid token"));
    }
}
