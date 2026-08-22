import { helper } from './util';

export class App {
    run(): number {
        return helper(1, 2);
    }
}

export class Child extends App {
    extra(): number {
        return this.run();
    }
}
