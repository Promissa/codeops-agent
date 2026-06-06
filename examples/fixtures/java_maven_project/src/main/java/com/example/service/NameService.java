package com.example.service;

public final class NameService {
    public String normalize(String value) {
        if (value == null || value.isEmpty()) {
            return "";
        }
        return value.trim();
    }
}
