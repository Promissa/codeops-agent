package com.example.service;

public final class NameServiceTest {
    public void handlesEmptyString() {
        if (!"".equals(new NameService().normalize(""))) {
            throw new AssertionError("expected empty string");
        }
    }
}
