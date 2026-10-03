# Proje talimatları

## 1. Zorunlu: tek ekran, sayfa kaydırması yok

HealthOS arayüzü kullanılabilir ekran alanına sığmalıdır. Sayfanın kendisinde dikey veya yatay kaydırma oluşmaması en yüksek öncelikli arayüz gereksinimidir. Her tasarım ve davranış değişikliği bu şartı korumalıdır.

- Üst menü, kartlar, grafikler ve geçmiş paneli birlikte ekran yüksekliğine göre yerleşmelidir.
- Geçmiş panelinin sürüklenmesi sayfayı uzatmamalı; panel sürüklenen mesafe kadar açılıp bırakılan yükseklikte kalmalıdır.
- Uzun içerikler gerektiğinde ilgili panelin içinde kaydırılmalıdır; ana sayfa kaydırılmamalıdır.
- İçeriği kesmek veya erişilemez hale getirmek için yalnızca `overflow: hidden` kullanmak çözüm sayılmaz.
- Arayüz değişiklikleri masaüstü ve mobil ekranlarda kontrol edilmeli; ana sayfa taşması varsa iş tamamlanmış sayılmamalıdır.
