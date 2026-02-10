import admin from 'firebase-admin';

if (!admin.apps.length) {
    try {
        admin.initializeApp({
            credential: admin.credential.cert(JSON.parse(process.env.FIREBASE_SERVICE_ACCOUNT || '{}')),
            projectId: process.env.PUBLIC_FIREBASE_PROJECT_ID
        });
        console.log('Firebase Admin initialized');
    } catch (error) {
        // Fallback for environments where service account is injected via other means (e.g. Vercel)
        admin.initializeApp();
        console.log('Firebase Admin initialized (Fallback)');
    }
}

export const db = admin.firestore();
export const auth = admin.auth();
