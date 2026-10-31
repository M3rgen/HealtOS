# İlk yükleme bütçesi

`frontend/performance-budget.json` ilk yükleme için sabit gzip tavanlarını bayt
cinsinden tutar: JS **60.000**, CSS **20.000**. Tavanlar ilk eklendikleri çevrimdışı
ortamda (kilitli Vite 8.3.1 önbellekte yoktu) ölçümsüz konmuştu; aşağıdaki ölçüm
onları sonradan doğruladı.

Ölçüm (25 Ekim 2026, macOS, Node 26, `npm ci && npx vite build`, kilitli Vite 8.3.1;
değerler budget eklentisinin `performance-budget-report.json` raporundan):

| İlk yükleme | Ham | gzip | Tavan | Pay |
| --- | ---: | ---: | ---: | ---: |
| JS | 155.843 B | **53.258 B** | 60.000 B | 6.742 B (%11) |
| CSS | 84.741 B | **16.703 B** | 20.000 B | 3.297 B (%16) |

Ertelenen beden/three.js parçası (sınır dışı, yalnız raporlanır): JS gzip 165.095 B,
CSS gzip 156 B. Pay, küçük arayüz değişikliklerini geçirecek ama yeni bir büyük
bağımlılığın ya da beden/three.js'in başlangıç grafiğine sızmasını yakalayacak kadar
dar. Tavanı yükseltmek gerekirse yeni ölçüm bu tabloya aynı yöntemle işlenmeli.

`build-budget.js` Vite'ın üretim eklentisidir; npm betiğine ya da sabit çıktı
dizinine bağlı değildir. `npm run build`, `npx vite build`, CI'ın `--outDir`
derlemesi ve tarayıcı koşucusunun programatik derlemesi aynı denetimden geçer.
Giriş HTML dosyalarının module script, modulepreload ve stylesheet köklerinden
statik importlar, parça CSS bağımlılıkları ve yerel CSS importları izlenir.
Ortak dosyalar bir kez sayılır. Model, görsel, font ve kaynak haritaları sayılmaz.

Rapor her grup için ham ve gzip (seviye 9, dosya başına) baytlarını ayrı verir.
Geçen derlemelerde aynı değerler `performance-budget-report.json` çıktısına yazılır.
Yalnız ilk yüklemenin JS/CSS **gzip** toplamları sınırlandırılır; eşitlik geçer,
aşım ölçüm/sınır içeren hatayla derlemeyi durdurur. Beden/three.js parçaları ve
bağımlılıkları ayrı raporlanır. Modül kimliklerinden bulunan beden veya three.js
statik başlangıç grafiğine girerse boyutundan bağımsız hata oluşur.

`npm run test:unit` grafik/sınır testlerini, `npm run test:build` gerçek Vite ile
kontrollü JS/CSS aşımını ve alternatif çıktı dizinini sınar. İkisi de
`make test-browser` içindedir. Tarayıcı testleri görünürlük, bekleyen ölçümler,
hareket tercihi, odak ve yükleme hatasında çalışan arayüzü kapsar.
