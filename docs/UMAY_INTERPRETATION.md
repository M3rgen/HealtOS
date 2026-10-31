# Umay yorum aracı — sürüm 2

`health_archive` içindeki `baseline`, `analyze`, `sleep`, `intraday`, `sessions`,
`physiology`, `longitudinal`, `relationship` eylemleri `read_health` ve sohbet API'si
üzerinden çalışır. `inventory`, `daily`, `records`, `coverage` sürüm 1 davranışını korur.
Kaynak arşivi ve manuel kayıtlar değişmez; yalnız mevcut, atılabilir günlük önbellek yazılır.

- Kişisel taban: varsayılan 56 önceki takvim günü (14–90 seçilebilir), en az 14 uygun gün
  ve %50 takvim desteği gerekir. 7 günün altı `cold_start` olur. Değerlendirilen gün ve
  sonrası tabana girmez. Medyan, ölçeklenmemiş MAD, min/max, doğrusal p10/p90 ve orta sıra
  yüzdeliği döner. Z = (değer − medyan)/(1,4826 × MAD); sıfır MAD'de z yoktur.
- Boşluk, belirsiz uyku, çelişki, kısmi gün ve eksik adım aralığı kıyas desteği sayılmaz.
  Ölçülmüş sıfır korunur. Eksik günler sıfıra çevrilmez. Kaynak seçim sıralaması yapılmaz.
  Yeniden adlandırma yalnız mevcut `raw_source_devices` üyelik kanıtıyla bağlanır;
  kayıt yöntemi ve ölçüm tanımı ayrı kalır. Kanıtsız kaynaklar birleştirilmez.
- `analyze.candidates`: uygun özetlerden orta sıraya uzaklığı en az 40 yüzdelik puan olan
  en çok beş gözlem adayı; skor azalan, eşitlikte özet sırası. Klinik önem/tedavi puanı
  değildir. `summary_index` tüm sonuç dizisine başvurur; `summary_offset` sayfanın başlangıcıdır.
- Uyku mevcut oturum/evre kanıtını aynen taşır. Evre toplamı zaman çizelgesi üretmez;
  desteklenmeyen çizelge `null`, bilinmeyen ve çakışan süreler ayrı kalır.
- Nabız: yalnız arası en çok 300 saniye olan örnekler arasında doğrusal süre tahmini;
  uçlara uzatma yoktur. Ortalama gerçek UTC saniyeleriyle ağırlıklıdır. Örnek dağılımı
  yalnız günün içindeki örneklerden gelir. Adım hızı aralığa eşit dağıtım tahminidir;
  kaynak aralık sayılarının istatistiği ayrı birimle verilir, kesin hareket patlaması yoktur.
- Oturumun fiziksel süresi, bildirilen aktif süre, mesafe/enerji ve çakışan oturumlar
  ayrıdır. Segment kanıtı olmadığından kesintisiz koşu süresi `null` olur.
- Fizyoloji yalnız mevcut parser alanlarını destekler; yöntem ayrıntısı/tanım/birim
  taşınır. Güncellik, sorgunun son gününe göre 7 gün; VO₂ için 30 gündür. Bu eşikler
  klinik geçerlilik sınırı değildir. Yokluk ve desteklenmeyen biçim ayrı nedenlerdir.
- Miktar özetlerinde aktif/toplam enerji ve kaynak aralıkları ayrıdır; farklı aralık süreleri ayrı tanımdır; aralık tutarları
  günlük toplam sayılmaz veya pencereye kırpılmaz. Kilo/beden değişimi aynı kaynak,
  tanım ve yöntemde hesaplanır; aynı andaki çelişkiler hesap dışında, kanıtta görünürdür.
  Test işaretli manuel ölçümler hem hesap hem kanıttan ortak okuyucuyla dışlanır.
- İlişki: önceden seçilmiş en çok beş −14…14 günlük gecikme, pozitif gecikmede Y sonra.
  Takvim çiftleri ve bağlı kayıt başvuruları döner. En az 20 çift ve %50 destek gerekir.
  Spearman orta sıraları kullanılır; pencerenin iki takvim yarısında en az 8 çift, aynı işaret ve
  en çok 0,35 katsayı farkı aranır. Sabit/kararsız seriden uygun bulgu üretilmez;
  p-değeri, nedensellik veya karıştırıcı kontrolü iddiası yoktur.

Yeni eylemler 90 gün, ilişki 180 gün; giriş 50000 ilgili kayıt ve 16 MiB ham JSON, sonuç 1000 özet,
ilişki 16 rejim, gönderilen sayfa 64 KiB ile sınırlıdır. Bütçe aşımı yapılandırılmış ret
üretir; JSON kesilmez. `next_cursor`, sürüm/sorgu/giriş ve tüm hesap sonucuna bağlıdır.
`limit` değişebilir; diğer parametreler aynı kalır. Değer, üyelik, silinme, karantina,
cihaz kararı veya ilgili manuel işaret değişiminde `stale_cursor` ile baştan başlanır.
İstatistik ve aday sırası sayfa boyutundan etkilenmez. PostgreSQL okuması tek işlem
anlık görüntüsü kullanır; gerçek PostgreSQL ve Google/model doğrulaması dış ortam işidir.

Giriş bütçesi eylemin kullandığı türlere uygulanır; örneğin dakikalık nabız arşivi kişisel
günlük tabanı engellemez. Önbellek kullanan eylemlerde beş günlük adaptörün girdileri
sayılır. Yoğun arşivde `lookback_days` de daraltılabilir; ret eksik veri bulgusu değildir.
