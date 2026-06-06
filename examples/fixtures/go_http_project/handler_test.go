package gohttpproject

import "testing"

func TestStatusForQueryRejectsEmptyValue(t *testing.T) {
	if statusForQuery("") != 400 {
		t.Fatal("expected bad request status")
	}
}
