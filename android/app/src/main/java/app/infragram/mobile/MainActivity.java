package app.infragram.mobile;

import android.annotation.SuppressLint;
import android.app.AlertDialog;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.text.InputType;
import android.view.Menu;
import android.view.MenuItem;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.Toast;

import androidx.activity.OnBackPressedCallback;
import androidx.appcompat.app.AppCompatActivity;

/**
 * Infragram для Android — тонкая обёртка WebView вокруг веб-приложения (мини-аппа).
 *
 * Адрес сервера задаётся один раз при первом запуске (и меняется в меню), чтобы
 * один и тот же APK работал для любого развёртывания без пересборки. Вход вне
 * Telegram обеспечивает само веб-приложение: связывание устройства кодом из бота
 * (/pair) хранится в DOM storage WebView — поэтому DOM storage включён.
 */
public class MainActivity extends AppCompatActivity {

    private static final String PREFS = "infragram";
    private static final String KEY_URL = "server_url";

    private WebView web;

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.parseColor("#0f172a"));
        web = new WebView(this);
        root.addView(web);
        setContentView(root);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);            // localStorage — для токена устройства
        s.setDatabaseEnabled(true);
        s.setJavaScriptCanOpenWindowsAutomatically(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);

        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
                Uri u = req.getUrl();
                String host = u.getHost();
                String scheme = u.getScheme();
                // Свой сервер — открываем внутри; внешнее (t.me, чужие ссылки,
                // tg://) — в системном браузере/приложении Telegram.
                String appHost = Uri.parse(currentUrl()).getHost();
                if (host != null && appHost != null && host.equals(appHost)) {
                    return false;   // грузим в WebView
                }
                if ("http".equals(scheme) || "https".equals(scheme)
                        || "tg".equals(scheme) || "mailto".equals(scheme)) {
                    try {
                        startActivity(new Intent(Intent.ACTION_VIEW, u));
                    } catch (Exception e) {
                        Toast.makeText(MainActivity.this,
                                "Не удалось открыть ссылку", Toast.LENGTH_SHORT).show();
                    }
                    return true;
                }
                return false;
            }
        });

        getOnBackPressedDispatcher().addCallback(this, new OnBackPressedCallback(true) {
            @Override public void handleOnBackPressed() {
                if (web.canGoBack()) web.goBack();
                else finish();
            }
        });

        String url = currentUrl();
        if (url == null || url.isEmpty()) askServerUrl(true);
        else web.loadUrl(url);
    }

    private String currentUrl() {
        SharedPreferences p = getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        return p.getString(KEY_URL, "");
    }

    /** Привести введённое к «https://host/miniapp/». */
    private String normalize(String raw) {
        if (raw == null) return "";
        String v = raw.trim();
        if (v.isEmpty()) return "";
        if (!v.startsWith("http://") && !v.startsWith("https://")) v = "https://" + v;
        while (v.endsWith("/")) v = v.substring(0, v.length() - 1);
        if (!v.contains("/miniapp")) v = v + "/miniapp/";
        else if (!v.endsWith("/")) v = v + "/";
        return v;
    }

    private void askServerUrl(final boolean firstRun) {
        final EditText in = new EditText(this);
        in.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        in.setHint("https://ваш-сервис.up.railway.app");
        String cur = currentUrl();
        in.setText(cur.isEmpty() ? "https://" : cur);

        new AlertDialog.Builder(this)
                .setTitle("Адрес сервера Infragram")
                .setMessage("Введите адрес вашего Infragram (тот же, что открывает "
                        + "бот). Достаточно домена — /miniapp добавится сам.")
                .setView(in)
                .setCancelable(!firstRun)
                .setPositiveButton("Сохранить", (d, w) -> {
                    String url = normalize(in.getText().toString());
                    if (url.isEmpty()) {
                        Toast.makeText(this, "Пустой адрес", Toast.LENGTH_SHORT).show();
                        askServerUrl(firstRun);
                        return;
                    }
                    getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                            .edit().putString(KEY_URL, url).apply();
                    web.loadUrl(url);
                })
                .show();
    }

    @Override
    public boolean onCreateOptionsMenu(Menu menu) {
        menu.add(0, 1, 0, "Обновить");
        menu.add(0, 2, 1, "Изменить адрес сервера");
        return true;
    }

    @Override
    public boolean onOptionsItemSelected(MenuItem item) {
        if (item.getItemId() == 1) { web.reload(); return true; }
        if (item.getItemId() == 2) { askServerUrl(false); return true; }
        return super.onOptionsItemSelected(item);
    }
}
