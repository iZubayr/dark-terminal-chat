# Dark Terminal

Mr. Robot kayfiyatidagi haqiqiy terminal chat: qora fon, yashil yozuv, laqab va maxfiy kirish kodi. Windows, macOS va Linux. Node.js 22 yoki yangirog‘i kerak; qo‘shimcha paket o‘rnatilmaydi.

## Bitta kompyuterda sinash

Uchta terminalni shu papkada oching.

1. Birinchi terminalda server:

   ```powershell
   npm run server
   ```

2. Ikkinchi terminalda yangi suhbat:

   ```powershell
   npm start -- --new --name elliot
   ```

   Ulangach, **KIRISH KODI** chiqadi. Uni nusxalang.

3. Uchinchi terminalda sherigingiz:

   ```powershell
   npm start -- --name whiterose
   ```

   `2` ni tanlang va berilgan kirish kodini kiriting. Endi yozib, Enter bosing.

Windowsda `server.cmd` va `chat.cmd` fayllarini ochish ham mumkin. Oddiy `chat.cmd` ichida laqab va yangi suhbat/kod bilan kirish so‘raladi. Terminal profilida qora fonni tanlang; yozuv rangi dastur tomonidan beriladi.

## Bir xil Wi-Fi orqali

Server ishlaydigan kompyuterda:

```powershell
npm run server -- --host 0.0.0.0
```

`ipconfig` orqali Wi-Fi adapterining IPv4 manzilini toping, masalan `192.168.1.20`. Windows Firewall so‘rasa, xususiy tarmoq uchun ruxsat bering. Ikkala ishtirokchi shu serverga ulanadi:

```powershell
npm start -- --host 192.168.1.20 --new --name elliot
npm start -- --host 192.168.1.20 --name whiterose
```

Ikkinchi buyruq boshqa terminal/kompyuterda bajariladi. Unga birinchi ishtirokchi olgan kod beriladi.

## Internet orqali

Ikkala ishtirokchi kira oladigan server kerak. Ushbu loyiha o‘zidan o‘zi internetga joylanmaydi; mahalliy `192.168.*` manzil boshqa joydan ochilmaydi.

Internetdagi VPS/serverga fayllarni ko‘chiring va Node.js bilan relayni ishga tushiring. Masalan, domeningiz uchun mavjud TLS sertifikati bilan:

```sh
node server.mjs --host 0.0.0.0 --port 4040 --cert /path/fullchain.pem --key /path/privkey.pem
```

Server xavfsizlik devorida TCP 4040 ochiq bo‘lishi kerak. Ikkala terminalda:

```sh
node chat.mjs --host chat.example.com --port 4040 --tls --new --name elliot
node chat.mjs --host chat.example.com --port 4040 --tls --name whiterose
```

`chat.example.com` o‘rniga o‘zingizning server domeningizni yozing. Shaxsiy CA bo‘lsa, `--tls --ca ca.pem` ishlating. Sertifikat tekshiruvi o‘chirilmaydi. VPS/TLS va turli joydagi haqiqiy ikki foydalanuvchi sinovi alohida bajarilishi kerak.

## Buyruqlar

| Buyruq | Vazifasi |
| --- | --- |
| `/help` | Buyruqlarni ko‘rsatish |
| `/who` | Tanishgan ishtirokchilarni ko‘rsatish |
| `/clear` | Ekranni tozalash |
| `/exit` yoki Ctrl+C | Chiqish |

## Maxfiylik chegaralari

- Yangi suhbatga 32 bayt tasodifiy maxfiy kod yaratiladi. Koddan xabar kaliti va alohida xona identifikatori olinadi.
- Xabar matni va laqablar mijozda AES-256-GCM bilan shifrlanadi. Relayga xona identifikatori va shifrlangan xabarlar keladi; kirish kodi yuborilmaydi.
- Kodni bilgan odam xabarni o‘qiy oladi va istalgan laqabni qo‘ya oladi. Laqab tasdiqlangan shaxs emas. Kodni ishonchli yo‘l bilan sherigingizga bering.
- Relay ulanish manzillari, xona identifikatori, xabar vaqti va hajmini ko‘radi. TLS ishlatilmasa, tarmoqdagi kuzatuvchi ham xona identifikatorini ko‘rishi, oqimga xalaqit berishi yoki paketlarni qayta uzatishi mumkin.
- Xabarlar va kod faylga yozilmaydi. Terminal scrollbackida matn va yaratilgan kod qolishi mumkin. `/clear` tarixni xavfsiz o‘chirish kafolati emas.
- Bu kichik shaxsiy loyiha; mustaqil xavfsizlik auditi, forward secrecy va haqiqiy shaxsni tasdiqlash mavjud emas. Anonimlik kafolatlanmaydi.
- Yangi kelgan odam oldingi xabarlarni olmaydi. Server o‘chsa ulanishlar uziladi; qayta kirish uchun chatni qayta ishga tushiring.
- Xabar uzunligi 2000 belgi. Relay bitta ulanishdan 10 soniyada 40 tagacha protokol paketini qabul qiladi va sekin mijozlarni uzadi.

## Tekshirish

```powershell
npm test
```

Node.js API manbalari: [crypto](https://nodejs.org/api/crypto.html), [readline](https://nodejs.org/api/readline.html), [net](https://nodejs.org/api/net.html).
