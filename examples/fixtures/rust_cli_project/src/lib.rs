pub fn parse_token(value: &str) -> Result<&str, &'static str> {
    if value.is_empty() {
        Err("invalid token")
    } else {
        Ok(value)
    }
}
