// PHASE: 1.3.2
package com.citi.qa.locatorforge.ui.api;

import org.json.JSONArray;
import org.json.JSONObject;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;

public final class ApiClient {

    private final String baseUrl;
    private final HttpClient http;

    public ApiClient(String baseUrl) {
        this.baseUrl = baseUrl;
        // Pin HTTP/1.1 — uvicorn doesn't speak h2c, and the JDK client's
        // default HTTP/2 upgrade attempt confuses it.
        this.http = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(5))
                .version(HttpClient.Version.HTTP_1_1)
                .build();
    }

    public String baseUrl() {
        return baseUrl;
    }

    public JSONObject getHealth() throws Exception {
        return getJson("/health");
    }

    public JSONObject getTree() throws Exception {
        return getJson("/tree");
    }

    public JSONObject push(String pomFile, String framework, JSONArray modifications) throws Exception {
        JSONObject body = new JSONObject();
        body.put("pom_file", pomFile);
        body.put("pom_framework", framework);
        body.put("modifications", modifications);
        return postJson("/push", body);
    }

    // ---- helpers ----
    private JSONObject getJson(String path) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(baseUrl + path))
                .timeout(Duration.ofSeconds(60))
                .GET()
                .build();
        HttpResponse<String> resp = http.send(req, HttpResponse.BodyHandlers.ofString());
        if (resp.statusCode() / 100 != 2) {
            throw new RuntimeException("GET " + path + " failed: " + resp.statusCode() + " " + resp.body());
        }
        return new JSONObject(resp.body());
    }

    private JSONObject postJson(String path, JSONObject body) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(baseUrl + path))
                .timeout(Duration.ofSeconds(60))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body.toString()))
                .build();
        HttpResponse<String> resp = http.send(req, HttpResponse.BodyHandlers.ofString());
        if (resp.statusCode() / 100 != 2) {
            throw new RuntimeException("POST " + path + " failed: " + resp.statusCode() + " " + resp.body());
        }
        return new JSONObject(resp.body());
    }
}
