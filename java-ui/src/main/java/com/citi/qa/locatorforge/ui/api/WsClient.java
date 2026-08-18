// PHASE: 2.2.4
package com.citi.qa.locatorforge.ui.api;

import org.json.JSONObject;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.WebSocket;
import java.util.concurrent.CompletionStage;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Consumer;

/**
 * Lightweight WebSocket client for {@code ws://127.0.0.1:{port}/ws}.
 * Reconnect with exponential backoff: 1s → 2s → 4s → 30s (cap).
 */
public final class WsClient {

    private final String url;
    private final Consumer<JSONObject> onEvent;
    private final HttpClient http = HttpClient.newHttpClient();
    private final AtomicReference<WebSocket> ws = new AtomicReference<>();
    private volatile boolean running = false;
    private volatile int backoffMs = 1000;

    public WsClient(String wsUrl, Consumer<JSONObject> onEvent) {
        this.url = wsUrl;
        this.onEvent = onEvent;
    }

    public void start() {
        running = true;
        connect();
    }

    public void stop() {
        running = false;
        WebSocket w = ws.getAndSet(null);
        if (w != null) {
            w.sendClose(WebSocket.NORMAL_CLOSURE, "shutdown");
        }
    }

    private void connect() {
        http.newWebSocketBuilder()
                .buildAsync(URI.create(url), new Listener())
                .whenComplete((sock, err) -> {
                    if (err != null) {
                        scheduleReconnect();
                    } else {
                        ws.set(sock);
                        backoffMs = 1000;
                    }
                });
    }

    private void scheduleReconnect() {
        if (!running) return;
        int wait = backoffMs;
        backoffMs = Math.min(backoffMs * 2, 30_000);
        new Thread(() -> {
            try { Thread.sleep(wait); } catch (InterruptedException ignored) { return; }
            if (running) connect();
        }, "lf-ws-reconnect").start();
    }

    private final class Listener implements WebSocket.Listener {
        private final StringBuilder buf = new StringBuilder();

        @Override public void onOpen(WebSocket webSocket) {
            webSocket.request(1);
        }

        @Override public CompletionStage<?> onText(WebSocket webSocket, CharSequence data, boolean last) {
            buf.append(data);
            if (last) {
                try {
                    onEvent.accept(new JSONObject(buf.toString()));
                } catch (Exception ignored) {
                } finally {
                    buf.setLength(0);
                }
            }
            webSocket.request(1);
            return null;
        }

        @Override public CompletionStage<?> onClose(WebSocket webSocket, int statusCode, String reason) {
            ws.set(null);
            scheduleReconnect();
            return null;
        }

        @Override public void onError(WebSocket webSocket, Throwable error) {
            ws.set(null);
            scheduleReconnect();
        }
    }
}
