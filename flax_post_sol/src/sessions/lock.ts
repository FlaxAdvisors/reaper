/* back/src/sessions/lock.ts */
export class LockManager {
    private holder: string | null = null;

    acquire(clientId: string): boolean {
        if (!this.holder) {
            this.holder = clientId;
            return true;
        }
        return false;
    }

    release(clientId: string): boolean {
        if (this.holder === clientId) {
            this.holder = null;
            return true;
        }
        return false;
    }

    status(): string | null {
        return this.holder;
    }

    isHeld(): boolean {
        return this.holder !== null;
    }

    isHeldBy(clientId: string): boolean {
        return this.holder === clientId;
    }
}
