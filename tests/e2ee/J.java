import java.io.*;
import java.math.BigInteger;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.security.*;
import java.security.spec.*;
import java.util.*;
import java.util.regex.*;
import javax.crypto.*;
import javax.crypto.spec.*;

/** Проверка того же набора JCA-вызовов, что использует Android-реализация (Crypto.kt). */
public class J {
    static final int CH = 1 << 20;
    static byte[] U(String s) { return s.getBytes(StandardCharsets.UTF_8); }
    static String b64(byte[] b) { return Base64.getEncoder().encodeToString(b); }
    static byte[] unb64(String s) { return Base64.getDecoder().decode(s); }
    static byte[] cat(byte[]... p) { ByteArrayOutputStream o = new ByteArrayOutputStream(); for (byte[] x : p) o.writeBytes(x); return o.toByteArray(); }
    static byte[] hmac(byte[] key, byte[] data) throws Exception { Mac m = Mac.getInstance("HmacSHA256"); m.init(new SecretKeySpec(key, "HmacSHA256")); return m.doFinal(data); }
    /** HKDF-SHA256 (RFC 5869), пустая соль = 32 нулевых байта, длина <= 32. */
    static byte[] hkdf(byte[] ikm, String info) throws Exception {
        byte[] prk = hmac(new byte[32], ikm);
        return hmac(prk, cat(U(info), new byte[]{1}));
    }
    static byte[] hkdfB(byte[] ikm, byte[] info) throws Exception { byte[] prk = hmac(new byte[32], ikm); return hmac(prk, cat(info, new byte[]{1})); }
    static byte[] pbkdf2(String pw, byte[] salt, int it) throws Exception {
        return SecretKeyFactory.getInstance("PBKDF2WithHmacSHA256").generateSecret(new PBEKeySpec(pw.toCharArray(), salt, it, 256)).getEncoded();
    }
    static byte[] gcm(boolean enc, byte[] key, byte[] nonce, byte[] data, byte[] aad) throws Exception {
        Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
        c.init(enc ? Cipher.ENCRYPT_MODE : Cipher.DECRYPT_MODE, new SecretKeySpec(key, "AES"), new GCMParameterSpec(128, nonce));
        c.updateAAD(aad);
        return c.doFinal(data);
    }
    static PublicKey pub(byte[] spki) throws Exception { return KeyFactory.getInstance("EC").generatePublic(new X509EncodedKeySpec(spki)); }
    static PrivateKey priv(byte[] p8) throws Exception { return KeyFactory.getInstance("EC").generatePrivate(new PKCS8EncodedKeySpec(p8)); }
    static byte[] ecdh(PrivateKey p, PublicKey q) throws Exception { KeyAgreement ka = KeyAgreement.getInstance("ECDH"); ka.init(p); ka.doPhase(q, true); return ka.generateSecret(); }

    // ECDSA: JCA даёт DER, протокол использует raw r||s (64 байта)
    static byte[] derToRaw(byte[] der) {
        int off = 2; if ((der[1] & 0x80) != 0) off = 2 + (der[1] & 0x7f);
        byte[] out = new byte[64];
        for (int part = 0; part < 2; part++) {
            int len = der[off + 1] & 0xff; off += 2;
            int start = off, l = len;
            while (l > 32 && der[start] == 0) { start++; l--; }
            System.arraycopy(der, start, out, part * 32 + (32 - l), l);
            off += len;
        }
        return out;
    }
    static byte[] rawToDer(byte[] raw) {
        byte[] r = intBytes(Arrays.copyOfRange(raw, 0, 32)), s = intBytes(Arrays.copyOfRange(raw, 32, 64));
        int total = 4 + r.length + s.length;
        ByteArrayOutputStream o = new ByteArrayOutputStream();
        o.write(0x30); if (total > 127) o.write(0x81); o.write(total);
        o.write(0x02); o.write(r.length); o.writeBytes(r); o.write(0x02); o.write(s.length); o.writeBytes(s);
        return o.toByteArray();
    }
    static byte[] intBytes(byte[] v) { byte[] b = new BigInteger(1, v).toByteArray(); return b; }
    static byte[] sign(PrivateKey k, byte[] data) throws Exception { Signature s = Signature.getInstance("SHA256withECDSA"); s.initSign(k); s.update(data); return derToRaw(s.sign()); }
    static boolean verify(PublicKey k, byte[] raw, byte[] data) throws Exception { Signature s = Signature.getInstance("SHA256withECDSA"); s.initVerify(k); s.update(data); return s.verify(rawToDer(raw)); }

    static byte[] chunkNonce(byte[] base, int i) { byte[] n = base.clone(); n[8] ^= (byte)(i >>> 24); n[9] ^= (byte)(i >>> 16); n[10] ^= (byte)(i >>> 8); n[11] ^= (byte) i; return n; }
    static byte[] chunkAad(int i, boolean last) { return cat(U("FRDM-CHUNK-v1"), new byte[]{(byte)(i>>>24),(byte)(i>>>16),(byte)(i>>>8),(byte)i}, new byte[]{(byte)(last?1:0)}); }
    static byte[] fileDecrypt(byte[] enc, byte[] key, byte[] base) throws Exception {
        int step = CH + 16, n = Math.max(1, (enc.length + step - 1) / step);
        ByteArrayOutputStream o = new ByteArrayOutputStream();
        for (int i = 0; i < n; i++) o.writeBytes(gcm(false, key, chunkNonce(base, i), Arrays.copyOfRange(enc, i * step, Math.min(enc.length, (i + 1) * step)), chunkAad(i, i == n - 1)));
        return o.toByteArray();
    }
    static byte[] fileEncrypt(byte[] data, byte[] key, byte[] base) throws Exception {
        int n = Math.max(1, (data.length + CH - 1) / CH);
        ByteArrayOutputStream o = new ByteArrayOutputStream();
        for (int i = 0; i < n; i++) o.writeBytes(gcm(true, key, chunkNonce(base, i), Arrays.copyOfRange(data, i * CH, Math.min(data.length, (i + 1) * CH)), chunkAad(i, i == n - 1)));
        return o.toByteArray();
    }
    static String jsonField(String json, String key) { Matcher m = Pattern.compile("\"" + key + "\"\\s*:\\s*\"([^\"]*)\"").matcher(json); if (!m.find()) throw new RuntimeException("no " + key); return m.group(1); }
    static byte[] sha256(byte[]... p) throws Exception { MessageDigest d = MessageDigest.getInstance("SHA-256"); return d.digest(cat(p)); }

    public static void main(String[] a) throws Exception {
        Map<String, String> kv = new HashMap<>();
        for (String line : Files.readAllLines(Path.of("/tmp/for_java.txt"), StandardCharsets.UTF_8)) { int i = line.indexOf('='); kv.put(line.substring(0, i), line.substring(i + 1)); }
        int iters = Integer.parseInt(kv.get("iters"));

        // 1) пароль -> auth/kek, блоб (PKCS8 из WebCrypto)
        byte[] master = pbkdf2(kv.get("bobpw"), U("freedom-e2ee-v1:bob"), iters);
        if (!b64(hkdf(master, "auth")).equals(kv.get("bobauth"))) throw new RuntimeException("auth mismatch");
        byte[] kek = hkdf(master, "kek");
        String blob = kv.get("bobblob");
        String plain = new String(gcm(false, kek, unb64(jsonField(blob, "n")), unb64(jsonField(blob, "c")), U("FRDM-KEYBLOB-v1|bob")), StandardCharsets.UTF_8);
        PrivateKey bobDh = priv(unb64(jsonField(plain, "dh"))), bobSig = priv(unb64(jsonField(plain, "sig")));
        if (!jsonField(plain, "dhPub").equals(kv.get("bobdh"))) throw new RuntimeException("dhPub mismatch");
        System.out.println("JAVA: KDF, блоб и PKCS8 из WebCrypto — OK");

        // 2) конверт от alice
        byte[] eph = unb64(kv.get("env_eph")), n = unb64(kv.get("env_n")), c = unb64(kv.get("env_c"));
        PublicKey aliceSig = pub(unb64(kv.get("alicesig")));
        if (!verify(aliceSig, unb64(kv.get("env_sig")), cat(U("FRDM-ENV-v1|7|3|alice|bob"), new byte[]{0}, eph, n, c))) throw new RuntimeException("envelope sig (WebCrypto raw -> DER) failed");
        byte[] info = U("FRDM-WRAP-v1|7|3|alice|bob");
        byte[] K = gcm(false, hkdfB(ecdh(bobDh, pub(eph)), info), n, c, info);
        if (!b64(K).equals(kv.get("K"))) throw new RuntimeException("K mismatch");
        System.out.println("JAVA: подпись WebCrypto (raw->DER) и ECDH-конверт — OK");

        // 3) сообщение
        byte[] mk = hkdf(K, "FRDM-MSG-KEY-v1"), ct = unb64(kv.get("msg_ct")), aad = U("FRDM-MSG-v1|7|3|alice");
        if (!verify(aliceSig, unb64(kv.get("msg_sig")), cat(aad, new byte[]{0}, ct))) throw new RuntimeException("msg sig");
        String text = new String(gcm(false, mk, Arrays.copyOfRange(ct, 0, 12), Arrays.copyOfRange(ct, 12, ct.length), aad), StandardCharsets.UTF_8);
        if (!text.contains("Привет, мир!")) throw new RuntimeException("text: " + text);
        System.out.println("JAVA: сообщение " + text);

        // 4) файл
        byte[] file = fileDecrypt(unb64(kv.get("file_enc")), unb64(kv.get("file_key")), unb64(kv.get("file_nonce")));
        if (!b64(sha256(file)).equals(kv.get("file_sha"))) throw new RuntimeException("file sha");
        System.out.println("JAVA: файл " + file.length + " байт — OK");

        // 5) сигнал и номер безопасности
        byte[] ck = hkdf(ecdh(bobDh, pub(unb64(kv.get("alicedh")))), "FRDM-CALL-v1|call-123");
        String sig = new String(gcm(false, ck, unb64(kv.get("signal_n")), unb64(kv.get("signal_c")), U("FRDM-SIG-v1|call-123|alice|bob")), StandardCharsets.UTF_8);
        if (!sig.contains("offer")) throw new RuntimeException("signal");
        byte[] fpA = sha256(U("FRDM-FP-v1|alice|"), unb64(kv.get("alicedh")), unb64(kv.get("alicesig")));
        byte[] fpB = sha256(U("FRDM-FP-v1|bob|"), unb64(kv.get("bobdh")), unb64(kv.get("bobsig")));
        byte[] h = sha256(fpA, fpB); StringBuilder sb = new StringBuilder();
        for (int i = 0; i < 8; i++) { long v = ((h[4*i] & 0xffL) << 24) | ((h[4*i+1] & 0xffL) << 16) | ((h[4*i+2] & 0xffL) << 8) | (h[4*i+3] & 0xffL); if (i > 0) sb.append(' '); sb.append(String.format("%05d", v % 100000)); }
        if (!sb.toString().equals(kv.get("safety"))) throw new RuntimeException("safety " + sb);
        System.out.println("JAVA: сигнал звонка и номер безопасности — OK");

        // 6) Java как отправитель: bob -> alice (envelope, сообщение, файл); проверят Python и Node
        KeyPairGenerator g = KeyPairGenerator.getInstance("EC"); g.initialize(new ECGenParameterSpec("secp256r1"));
        KeyPair e2 = g.generateKeyPair(); byte[] K2 = new byte[32]; new SecureRandom().nextBytes(K2);
        byte[] info2 = U("FRDM-WRAP-v1|11|1|bob|alice"), n2 = new byte[12]; new SecureRandom().nextBytes(n2);
        byte[] c2 = gcm(true, hkdfB(ecdh(e2.getPrivate(), pub(unb64(kv.get("alicedh")))), info2), n2, K2, info2);
        byte[] eph2 = e2.getPublic().getEncoded();
        byte[] envSig = sign(bobSig, cat(U("FRDM-ENV-v1|11|1|bob|alice"), new byte[]{0}, eph2, n2, c2));
        byte[] aad2 = U("FRDM-MSG-v1|11|1|bob"), mn = new byte[12]; new SecureRandom().nextBytes(mn);
        byte[] ct2 = cat(mn, gcm(true, hkdf(K2, "FRDM-MSG-KEY-v1"), mn, U("{\"v\":1,\"t\":\"text\",\"text\":\"из Java/Android\"}"), aad2));
        byte[] msgSig = sign(bobSig, cat(aad2, new byte[]{0}, ct2));
        byte[] data = new byte[CH + 77]; new SecureRandom().nextBytes(data); byte[] fk = new byte[32], fb = new byte[12]; new SecureRandom().nextBytes(fk); new SecureRandom().nextBytes(fb);
        List<String> out = List.of("env_eph=" + b64(eph2), "env_n=" + b64(n2), "env_c=" + b64(c2), "env_sig=" + b64(envSig), "K=" + b64(K2),
            "msg_ct=" + b64(ct2), "msg_sig=" + b64(msgSig), "file_enc=" + b64(fileEncrypt(data, fk, fb)), "file_key=" + b64(fk), "file_nonce=" + b64(fb), "file_sha=" + b64(sha256(data)));
        Files.write(Path.of("/tmp/java_out.txt"), out, StandardCharsets.UTF_8);
        // повторный самотест: DER<->raw на 2000 случайных подписях (короткие r/s с ведущими нулями)
        for (int i = 0; i < 2000; i++) { byte[] m = new byte[16]; new SecureRandom().nextBytes(m); if (!verify(bobSig == null ? null : pub(unb64(kv.get("bobsig"))), sign(bobSig, m), m)) throw new RuntimeException("sig roundtrip " + i); }
        System.out.println("JAVA: 2000 подписей DER<->raw (включая короткие r/s) — OK; артефакты для JS/Python созданы");
    }
}
