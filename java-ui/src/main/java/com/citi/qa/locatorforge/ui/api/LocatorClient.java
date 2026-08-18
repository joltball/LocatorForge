// PHASE: 2.1.3 + 2.1.4 + 2.1.5 + 2.1.6
package com.citi.qa.locatorforge.ui.api;

import org.json.JSONArray;
import org.json.JSONObject;

import java.net.URI;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;

/** Phase-2 endpoint wrappers, layered on top of {@link ApiClient}. */
public final class LocatorClient {

    private final ApiClient api;
    private final java.net.http.HttpClient http;

    public LocatorClient(ApiClient api) {
        this.api = api;
        // Pin HTTP/1.1 — uvicorn doesn't speak HTTP/2 over cleartext and the
        // JDK client's default h2c upgrade attempt on its first request makes
        // uvicorn log "Invalid HTTP request received" and reply 400.
        this.http = java.net.http.HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(5))
                .version(java.net.http.HttpClient.Version.HTTP_1_1)
                .build();
    }

    public JSONObject getLocators(String nodeId) throws Exception {
        return get("/locators/" + nodeId);
    }

    public JSONObject highlight(String nodeId) throws Exception {
        return post("/highlight/" + nodeId, new JSONObject());
    }

    public JSONObject pickElement() throws Exception {
        return post("/element/pick", new JSONObject());
    }

    /** Begin passive interaction recording. {@code clear} discards prior captures. */
    public JSONObject startRecording(boolean clear) throws Exception {
        JSONObject body = new JSONObject();
        body.put("clear", clear);
        return post("/record/start", body);
    }

    /** Stop recording and flush {@code .locatorforge/recording.json}. */
    public JSONObject stopRecording() throws Exception {
        return post("/record/stop", new JSONObject());
    }

    public JSONObject getRecording() throws Exception {
        return get("/record");
    }

    public JSONObject validate(String strategy, String value) throws Exception {
        JSONObject body = new JSONObject();
        body.put("strategy", strategy);
        body.put("value", value);
        body.put("shadow_chain", new JSONArray());
        return post("/validate", body);
    }

    private JSONObject get(String path) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(api.baseUrl() + path))
                .timeout(Duration.ofSeconds(60)).GET().build();
        HttpResponse<String> resp = http.send(req, HttpResponse.BodyHandlers.ofString());
        if (resp.statusCode() / 100 != 2) {
            throw new RuntimeException("GET " + path + " failed: " + resp.statusCode() + " " + resp.body());
        }
        return new JSONObject(resp.body());
    }

    private JSONObject post(String path, JSONObject body) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(api.baseUrl() + path))
                .timeout(Duration.ofSeconds(60))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body.toString())).build();
        HttpResponse<String> resp = http.send(req, HttpResponse.BodyHandlers.ofString());
        if (resp.statusCode() / 100 != 2) {
            throw new RuntimeException("POST " + path + " failed: " + resp.statusCode() + " " + resp.body());
        }
        return new JSONObject(resp.body());
    }
}
