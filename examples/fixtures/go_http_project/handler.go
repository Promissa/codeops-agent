package gohttpproject

func statusForQuery(value string) int {
	if value == "" {
		return 400
	}
	return 200
}
