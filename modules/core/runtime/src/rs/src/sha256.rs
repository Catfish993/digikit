//! SHA-256 (FIPS 180-4), streaming, 32-bit lengths only (module files are at most 64 KiB).

const K: [u32; 64] = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
];

pub struct Sha {
    h: [u32; 8],
    buf: [u8; 64],
    n: u32,
    total: u32,
}

impl Sha {
    pub fn new() -> Sha {
        Sha {
            h: [
                0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab,
                0x5be0cd19,
            ],
            buf: [0; 64],
            n: 0,
            total: 0,
        }
    }

    unsafe fn block(&mut self, p: *const u8) {
        let mut w = [0u32; 64];
        let mut i = 0;
        while i < 16 {
            let q = p.add(i * 4);
            w[i] = ((*q as u32) << 24)
                | ((*q.add(1) as u32) << 16)
                | ((*q.add(2) as u32) << 8)
                | (*q.add(3) as u32);
            i += 1;
        }
        while i < 64 {
            let a = w[i - 15];
            let b = w[i - 2];
            let s0 = ((a >> 7) | (a << 25)) ^ ((a >> 18) | (a << 14)) ^ (a >> 3);
            let s1 = ((b >> 17) | (b << 15)) ^ ((b >> 19) | (b << 13)) ^ (b >> 10);
            w[i] = w[i - 16]
                .wrapping_add(s0)
                .wrapping_add(w[i - 7])
                .wrapping_add(s1);
            i += 1;
        }
        let mut v = self.h;
        i = 0;
        while i < 64 {
            let e = v[4];
            let a = v[0];
            let s1 = ((e >> 6) | (e << 26)) ^ ((e >> 11) | (e << 21)) ^ ((e >> 25) | (e << 7));
            let ch = (e & v[5]) ^ (!e & v[6]);
            let t1 = v[7]
                .wrapping_add(s1)
                .wrapping_add(ch)
                .wrapping_add(K[i])
                .wrapping_add(w[i]);
            let s0 = ((a >> 2) | (a << 30)) ^ ((a >> 13) | (a << 19)) ^ ((a >> 22) | (a << 10));
            let maj = (a & v[1]) ^ (a & v[2]) ^ (v[1] & v[2]);
            let t2 = s0.wrapping_add(maj);
            v[7] = v[6];
            v[6] = v[5];
            v[5] = v[4];
            v[4] = v[3].wrapping_add(t1);
            v[3] = v[2];
            v[2] = v[1];
            v[1] = v[0];
            v[0] = t1.wrapping_add(t2);
            i += 1;
        }
        i = 0;
        while i < 8 {
            self.h[i] = self.h[i].wrapping_add(v[i]);
            i += 1;
        }
    }

    /// Feed `n` bytes from `p`.
    pub unsafe fn update(&mut self, mut p: *const u8, mut n: u32) {
        self.total = self.total.wrapping_add(n);
        while n > 0 {
            if self.n == 0 && n >= 64 {
                self.block(p);
                p = p.add(64);
                n -= 64;
                continue;
            }
            self.buf[self.n as usize] = *p;
            self.n += 1;
            p = p.add(1);
            n -= 1;
            if self.n == 64 {
                let b = self.buf.as_ptr();
                self.block(b);
                self.n = 0;
            }
        }
    }

    /// Feed `n` zero bytes.
    pub unsafe fn update_zeros(&mut self, n: u32) {
        let z = [0u8; 32];
        let mut left = n;
        while left > 0 {
            let k = if left > 32 { 32 } else { left };
            self.update(z.as_ptr(), k);
            left -= k;
        }
    }

    pub unsafe fn finish(mut self, out: *mut u8) {
        let bits_hi = self.total >> 29;
        let bits_lo = self.total << 3;
        let pad = [0x80u8];
        let zero = [0u8];
        self.update(pad.as_ptr(), 1);
        while self.n != 56 {
            self.update(zero.as_ptr(), 1);
        }
        let len = [
            (bits_hi >> 24) as u8,
            (bits_hi >> 16) as u8,
            (bits_hi >> 8) as u8,
            bits_hi as u8,
            (bits_lo >> 24) as u8,
            (bits_lo >> 16) as u8,
            (bits_lo >> 8) as u8,
            bits_lo as u8,
        ];
        self.update(len.as_ptr(), 8);
        let mut i = 0;
        while i < 8 {
            let x = self.h[i];
            *out.add(i * 4) = (x >> 24) as u8;
            *out.add(i * 4 + 1) = (x >> 16) as u8;
            *out.add(i * 4 + 2) = (x >> 8) as u8;
            *out.add(i * 4 + 3) = x as u8;
            i += 1;
        }
    }
}

/// SHA-256 of `n` bytes at `p` into `out[32]`.
pub unsafe fn sha256(p: *const u8, n: u32, out: *mut u8) {
    let mut s = Sha::new();
    s.update(p, n);
    s.finish(out);
}
